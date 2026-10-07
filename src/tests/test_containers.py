"""保管場所・ラベルID・箱の中身のテスト。"""

import sqlite3

import pytest

from okiba import containers, events, items
from okiba.common import ValidationError
from okiba.events import Event
from okiba.items import ContainerContents
from tests.helpers import KIND_BOX, Places, book_input, cable_input, set_status


class TestHierarchy:
    def test_path_shows_hierarchy_with_label(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 書斎 › 本棚 › 上段 › 箱A-01 / 操作: 表示名を得る / 期待: 階層とラベルIDを含む。"""
        assert containers.container_path(conn, places.box_a1) == '書斎 › 本棚 › 上段 › 箱 [A-01]'

    def test_list_orders_children_after_parent(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 階層つきの保管場所 / 操作: 一覧を得る / 期待: 親の直後に子が並ぶ。"""
        paths: list[str] = [path for _, path in containers.all_paths(conn)]
        assert paths.index('書斎 › 本棚') == paths.index('書斎') + 1
        assert paths.index('押入れ › 箱 [B-01]') == paths.index('押入れ') + 1

    @pytest.mark.parametrize('target', ['self', 'descendant'], ids=['自分自身', '配下'])
    def test_cannot_move_into_itself_or_descendant(self, conn: sqlite3.Connection, places: Places, target: str) -> None:
        """前提: 本棚の配下に上段 / 操作: 本棚を自分自身・上段の中へ移す / 期待: 拒否される。"""
        new_parent: int = places.bookcase if target == 'self' else places.upper_shelf
        with pytest.raises(ValidationError):
            containers.move_container(conn, places.bookcase, new_parent)

    def test_moving_box_records_only_box_event(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 書籍の入った箱A-01 / 操作: 箱を押入れへ移す / 期待: 箱の移動の履歴だけが残り、中身の履歴は増えない。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1))
        containers.move_container(conn, places.box_a1, places.closet)
        assert containers.container_path(conn, places.box_a1) == '押入れ › 箱 [A-01]'
        assert [event.kind for event in events.list_events(conn, 'container', places.box_a1)] == ['move', 'create']
        assert [event.kind for event in events.list_events(conn, 'item', item_id)] == ['create']

    def test_update_records_event_only_when_changed(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 箱A-01 / 操作: 同じ内容で保存し、次に名前を変える / 期待: 名前を変えたときだけ修正の履歴が残る。"""
        containers.update_container(conn, places.box_a1, '箱', KIND_BOX, 'A-01')
        containers.update_container(conn, places.box_a1, 'ケーブル箱', KIND_BOX, 'A-01')
        history: list[Event] = events.list_events(conn, 'container', places.box_a1)
        assert [event.kind for event in history] == ['update', 'create']
        assert ((history[0].before or {})['name'], (history[0].after or {})['name']) == ('箱', 'ケーブル箱')


class TestLabels:
    def test_label_is_normalized(self, conn: sqlite3.Connection) -> None:
        """前提: なし / 操作: 全角・小文字でラベルIDを入力する / 期待: 半角大文字で保存される。"""
        container_id: int = containers.create_container(conn, '箱', KIND_BOX, label='ｃ－０１')
        assert containers.get_container(conn, container_id).label == 'C-01'

    @pytest.mark.parametrize('label', ['A01', 'A-1', 'ABCD-01', 'あ-01'], ids=['ハイフンなし', '1桁', '英字4字', '英字以外'])
    def test_invalid_label_is_rejected(self, conn: sqlite3.Connection, label: str) -> None:
        """前提: なし / 操作: 形式に合わないラベルIDを入力する / 期待: 拒否される。"""
        with pytest.raises(ValidationError) as error:
            containers.create_container(conn, '箱', KIND_BOX, label=label)
        assert 'label' in error.value.errors

    def test_duplicate_label_is_rejected(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: A-01が使用済み / 操作: A-01で作成する / 期待: 拒否される。"""
        with pytest.raises(ValidationError):
            containers.create_container(conn, '箱', KIND_BOX, label='A-01')

    def test_next_label_is_suggested_per_prefix(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: A-01・A-02・B-01が使用済み / 操作: 次のラベルIDの候補を得る / 期待: 接頭辞ごとの次の番号。"""
        assert containers.suggest_label(conn, 'A') == 'A-03'
        assert containers.suggest_label(conn, 'b') == 'B-02'
        assert containers.suggest_label(conn, 'C') == 'C-01'

    def test_container_kind_can_be_added(self, conn: sqlite3.Connection) -> None:
        """前提: 初期の4種類 / 操作: 「引き出し」を追加し、同名を再度追加する / 期待: 追加され、同名は拒否される。"""
        containers.add_kind(conn, '引き出し')
        assert [kind.name for kind in containers.list_kinds(conn)][-1] == '引き出し'
        with pytest.raises(ValidationError):
            containers.add_kind(conn, '引き出し')


class TestContents:
    def test_contents_separate_stored_and_returning_items(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 箱A-01に保管中2件（計3本）、使用中・貸出中1件ずつ / 操作: 中身を得る / 期待: 保管中と戻す物に分かれ、状態別の件数・数量が出る。"""
        items.register_item(conn, cable_input(places.box_a1))
        items.register_item(conn, cable_input(places.box_a1, a='USB-A', quantity=2))
        in_use: int = items.register_item(conn, cable_input(places.box_a1, a='DVI'))
        lent: int = items.register_item(conn, book_input(places.box_a1))
        set_status(conn, in_use, 'in_use', places.box_a1)
        set_status(conn, lent, 'lent', places.box_a1)
        contents: ContainerContents = items.container_contents(conn, places.box_a1)
        assert len(contents.stored) == 2
        assert sorted(summary.status for summary in contents.returning) == ['in_use', 'lent']
        assert [(c.status, c.count, c.quantity) for c in contents.counts] == [('stored', 2, 3), ('in_use', 1, 1), ('lent', 1, 1)]

    def test_nested_contents_are_optional(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 箱A-01・A-02に1本ずつ / 操作: 上段の中身を、配下を含めずに・含めて得る / 期待: 含めた場合だけ2本出る。"""
        items.register_item(conn, cable_input(places.box_a1))
        items.register_item(conn, cable_input(places.box_a2))
        assert items.container_contents(conn, places.upper_shelf).stored == []
        assert len(items.container_contents(conn, places.upper_shelf, include_nested=True).stored) == 2

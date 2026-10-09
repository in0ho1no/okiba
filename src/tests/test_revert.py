"""履歴からの差し戻しのテスト。"""

import sqlite3
from typing import Any

import pytest

from okiba import events, items, revert, status
from okiba.common import NotFoundError, ValidationError
from okiba.events import Event
from okiba.items import ItemDetail
from okiba.revert import RevertPlan
from okiba.status import StatusRecord
from tests.helpers import BOOKS_ID, Places, book_input


def _latest(conn: sqlite3.Connection, item_id: int) -> Event:
    return events.list_events(conn, 'item', item_id)[0]


def _overwrite(conn: sqlite3.Connection, places: Places, item_id: int, **changes: Any) -> Event:
    """内容の修正で上書きし、その修正の履歴を返す。"""
    items.update_item(conn, item_id, book_input(places.box_a1, **changes))
    return _latest(conn, item_id)


class TestRevert:
    def test_only_descriptive_fields_are_reverted(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 名称・著者・タグ・備考・数量を上書きして貸し出した書籍 / 操作: 上書き前に差し戻す / 期待: 4項目だけ戻り、数量・状態は今のまま。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1, note='初版', tag_names=['技術書']))
        overwrite: Event = _overwrite(
            conn,
            places,
            item_id,
            name='誤った名前',
            note='',
            tag_names=['雑誌'],
            quantity=2,
            attributes={'author': '別人', 'publisher': 'オライリー・ジャパン'},
        )
        status.lend(conn, item_id, StatusRecord(date='2026-10-01', party='友人'))
        assert revert.apply_revert(conn, item_id, overwrite.id)
        item: ItemDetail = items.get_item(conn, item_id)
        assert (item.name, item.note, item.tags, item.attributes['author']) == ('リーダブルコード', '初版', ['技術書'], 'Dustin Boswell')
        assert (item.quantity, item.status, item.identifiers) == (2, 'lent', {'isbn': '9784873115658'})

    def test_plan_shows_differences_from_current(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 名称と備考を上書きした書籍 / 操作: 差し戻しの内容を確認する / 期待: 名称と備考の現在値と差し戻し後の値が示され、保存はされない。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1, note='初版'))
        overwrite: Event = _overwrite(conn, places, item_id, name='誤った名前', note='')
        plan: RevertPlan = revert.plan_revert(conn, item_id, overwrite.id)
        assert [(c.label, c.current, c.reverted) for c in plan.changes] == [('名称', '誤った名前', 'リーダブルコード'), ('備考', '', '初版')]
        assert items.get_item(conn, item_id).name == '誤った名前'

    def test_revert_is_recorded_as_new_update(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 名称を上書きした書籍 / 操作: 差し戻す / 期待: 修正の履歴が追加され、後の履歴は書き換わらない。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1))
        overwrite: Event = _overwrite(conn, places, item_id, name='誤った名前')
        revert.apply_revert(conn, item_id, overwrite.id)
        history: list[Event] = events.list_events(conn, 'item', item_id)
        assert [event.kind for event in history] == ['update', 'update', 'create']
        assert history[0].memo.startswith('履歴からの差し戻し')
        assert history[1] == overwrite

    def test_merged_tag_is_replaced_by_merge_target(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 「技術書」を外した書籍、その後「技術書」を「IT書籍」へ統合 / 操作: 差し戻す / 期待: 統合先の「IT書籍」が付く。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1, tag_names=['技術書']))
        overwrite: Event = _overwrite(conn, places, item_id, tag_names=[])
        items.register_item(conn, book_input(places.box_a2, tag_names=['IT書籍']))
        conn.execute("UPDATE tags SET retired = 1, merged_into_id = (SELECT id FROM tags WHERE name = 'IT書籍') WHERE name = '技術書'")
        revert.apply_revert(conn, item_id, overwrite.id)
        assert items.get_item(conn, item_id).tags == ['IT書籍']

    def test_retired_tag_is_restored_and_flagged(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 「売却候補」を外した書籍、その後「売却候補」を廃止 / 操作: 差し戻す / 期待: 廃止済みと示され、そのまま付く。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1, tag_names=['売却候補']))
        overwrite: Event = _overwrite(conn, places, item_id, tag_names=[])
        conn.execute("UPDATE tags SET retired = 1 WHERE name = '売却候補'")
        assert revert.plan_revert(conn, item_id, overwrite.id).retired_tags == ['売却候補']
        revert.apply_revert(conn, item_id, overwrite.id)
        assert items.get_item(conn, item_id).tags == ['売却候補']

    def test_empty_required_field_blocks_revert(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 著者を後から入力した書籍、その後著者を必須化 / 操作: 入力前に差し戻す / 期待: 必須項目のエラーで保存されない。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1, attributes={}))
        overwrite: Event = _overwrite(conn, places, item_id)
        conn.execute("UPDATE category_fields SET required = 1 WHERE category_id = ? AND key = 'author'", (BOOKS_ID,))
        assert 'attr__author' in revert.plan_revert(conn, item_id, overwrite.id).errors
        with pytest.raises(ValidationError):
            revert.apply_revert(conn, item_id, overwrite.id)
        assert items.get_item(conn, item_id).attributes['author'] == 'Dustin Boswell'

    def test_create_event_has_nothing_to_revert(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 登録しただけの書籍 / 操作: 登録の履歴に差し戻す / 期待: 変更前がないため拒否される。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1))
        with pytest.raises(ValidationError):
            revert.plan_revert(conn, item_id, _latest(conn, item_id).id)

    def test_event_of_other_item_is_not_found(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 書籍2冊 / 操作: 別の物品の履歴を指定して差し戻す / 期待: 見つからない扱いになる。"""
        first: int = items.register_item(conn, book_input(places.box_a1))
        second: int = items.register_item(conn, book_input(places.box_a2))
        overwrite: Event = _overwrite(conn, places, second, name='誤った名前')
        with pytest.raises(NotFoundError):
            revert.plan_revert(conn, first, overwrite.id)

    def test_deleted_item_cannot_be_reverted(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 名称を上書きしてから削除した書籍 / 操作: 差し戻す / 期待: 拒否される。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1))
        overwrite: Event = _overwrite(conn, places, item_id, name='誤った名前')
        status.delete_item(conn, item_id, '誤登録')
        with pytest.raises(ValidationError):
            revert.apply_revert(conn, item_id, overwrite.id)

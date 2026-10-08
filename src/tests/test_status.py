"""状態と手放し・手放しの取消・誤登録削除と復元のテスト。"""

import sqlite3

import pytest

from okiba import events, items, status
from okiba.common import ValidationError
from okiba.events import Event
from okiba.items import ItemDetail, SearchQuery
from okiba.status import StatusRecord
from tests.helpers import Places, book_input, retire_container

LEND: StatusRecord = StatusRecord(date='2026-10-01', party='佐藤さん', note='年末に返却予定')
SALE: StatusRecord = StatusRecord(date='2026-10-05', party='古書店', note='300円')


def _found(conn: sqlite3.Connection, text: str, include_deleted: bool = False) -> list[int]:
    query: SearchQuery = SearchQuery(text=text, statuses=(), include_deleted=include_deleted)
    return [summary.id for summary in items.search_items(conn, query)]


class TestTakeOutAndPutBack:
    def test_take_out_keeps_container_as_return_destination(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 箱A-01に保管中の書籍 / 操作: 取り出す / 期待: 使用中になり、箱A-01が戻し先になる。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1))
        status.take_out(conn, item_id)
        item: ItemDetail = items.get_item(conn, item_id)
        assert (item.status, item.container_id, item.is_return_target) == ('in_use', places.box_a1, True)

    def test_put_back_returns_to_stored(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 使用中の書籍 / 操作: 戻す / 期待: 戻し先に保管中として戻り、状態変更の履歴が2件残る。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1))
        status.take_out(conn, item_id)
        status.put_back(conn, item_id)
        assert (items.get_item(conn, item_id).status, items.get_item(conn, item_id).container_id) == ('stored', places.box_a1)
        kinds: list[str] = [event.kind for event in events.list_events(conn, 'item', item_id)]
        assert kinds == ['status', 'status', 'create']

    @pytest.mark.parametrize('operation', ['put_back', 'give_back', 'undo_release'])
    def test_operation_not_allowed_from_stored(self, conn: sqlite3.Connection, places: Places, operation: str) -> None:
        """前提: 保管中の書籍 / 操作: 戻す・返却・手放しの取消 / 期待: 状態に合わない操作として拒否され、履歴は増えない。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1))
        with pytest.raises(ValidationError) as error:
            getattr(status, operation)(conn, item_id)
        assert '保管中の物は' in error.value.errors['item']
        assert len(events.list_events(conn, 'item', item_id)) == 1


class TestLendAndGiveBack:
    def test_lend_records_date_party_and_note(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 使用中の書籍 / 操作: 貸し出す / 期待: 貸出中になり、日付・相手・備考と戻し先が残る。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1))
        status.take_out(conn, item_id)
        status.lend(conn, item_id, LEND)
        item: ItemDetail = items.get_item(conn, item_id)
        assert (item.status, item.container_id) == ('lent', places.box_a1)
        assert (item.status_date, item.status_party, item.status_note) == ('2026-10-01', '佐藤さん', '年末に返却予定')

    def test_lend_requires_valid_date_and_party(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 保管中の書籍 / 操作: 日付の形式が不正・相手が空で貸し出す / 期待: 入力欄ごとのエラーで拒否される。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1))
        with pytest.raises(ValidationError) as error:
            status.lend(conn, item_id, StatusRecord(date='10/1', party=' '))
        assert set(error.value.errors) == {'date', 'party'}
        assert items.get_item(conn, item_id).status == 'stored'

    def test_give_back_clears_lending_info_but_keeps_history(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 貸出中の書籍 / 操作: 返却する / 期待: 保管中に戻り、貸出の情報は空になるが履歴の変更前に残る。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1))
        status.lend(conn, item_id, LEND)
        status.give_back(conn, item_id)
        item: ItemDetail = items.get_item(conn, item_id)
        assert (item.status, item.container_id, item.status_party) == ('stored', places.box_a1, None)
        latest: Event = events.list_events(conn, 'item', item_id)[0]
        assert (latest.kind, latest.memo, (latest.before or {})['status_party']) == ('status', '返却', '佐藤さん')

    def test_lending_party_is_searchable_only_while_lent(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 書籍 / 操作: 貸し出してから返却する / 期待: 貸出中だけ貸出先の名前で見つかる。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1))
        status.lend(conn, item_id, LEND)
        assert _found(conn, '佐藤さん') == [item_id]
        status.give_back(conn, item_id)
        assert _found(conn, '佐藤さん') == []


class TestRelease:
    def test_sell_clears_container_and_keeps_item_searchable(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 保管中の書籍 / 操作: 売却する / 期待: 売却済で保管場所は空、売却先で検索でき、直前の場所は履歴に残る。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1))
        status.release(conn, item_id, 'sold', SALE)
        item: ItemDetail = items.get_item(conn, item_id)
        assert (item.status, item.container_id, item.status_party) == ('sold', None, '古書店')
        assert _found(conn, '古書店') == [item_id]
        latest: Event = events.list_events(conn, 'item', item_id)[0]
        assert (latest.before or {})['container_id'] == places.box_a1

    def test_release_event_snapshot_points_to_itself(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 保管中の書籍 / 操作: 廃棄する / 期待: 履歴の変更後に、取消の対象となる手放し操作の履歴IDが入る。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1))
        status.release(conn, item_id, 'disposed', StatusRecord(date='2026-10-05'))
        latest: Event = events.list_events(conn, 'item', item_id)[0]
        assert (latest.after or {})['release_event_id'] == latest.id

    def test_dispose_does_not_require_party_but_sell_does(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 保管中の書籍2冊 / 操作: 相手なしで廃棄・売却する / 期待: 廃棄はでき、売却は相手の入力を求められる。"""
        disposed: int = items.register_item(conn, book_input(places.box_a1))
        sold: int = items.register_item(conn, book_input(places.box_a1))
        status.release(conn, disposed, 'disposed', StatusRecord(date='2026-10-05'))
        with pytest.raises(ValidationError) as error:
            status.release(conn, sold, 'sold', StatusRecord(date='2026-10-05'))
        assert set(error.value.errors) == {'party'}

    def test_released_item_cannot_be_released_again(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 売却済の書籍 / 操作: 廃棄する / 期待: 拒否される。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1))
        status.release(conn, item_id, 'sold', SALE)
        with pytest.raises(ValidationError):
            status.release(conn, item_id, 'disposed', StatusRecord(date='2026-10-06'))


class TestUndoRelease:
    def test_undo_restores_lending_details(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 貸出中→売却済の書籍 / 操作: 手放しを取り消す / 期待: 貸出中に戻り、元の貸出日・貸出先・備考と戻し先も戻る。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1))
        status.lend(conn, item_id, LEND)
        status.release(conn, item_id, 'sold', SALE)
        status.undo_release(conn, item_id)
        item: ItemDetail = items.get_item(conn, item_id)
        assert (item.status, item.container_id) == ('lent', places.box_a1)
        assert (item.status_date, item.status_party, item.status_note) == ('2026-10-01', '佐藤さん', '年末に返却予定')

    def test_undo_keeps_later_edits_and_both_histories(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 売却後に備考を修正した書籍 / 操作: 手放しを取り消す / 期待: 保管中に戻り、備考の修正は残り、売却と取消の履歴が両方残る。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1))
        status.release(conn, item_id, 'sold', SALE)
        items.update_item(conn, item_id, book_input(places.box_a1, note='売却後のメモ'))
        status.undo_release(conn, item_id)
        item: ItemDetail = items.get_item(conn, item_id)
        assert (item.status, item.container_id, item.note, item.status_party) == ('stored', places.box_a1, '売却後のメモ', None)
        memos: list[str] = [event.memo for event in events.list_events(conn, 'item', item_id) if event.kind == 'status']
        assert memos == ['手放しの取消', '売却']

    def test_undo_requires_new_destination_when_original_is_retired(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 箱A-01から売却、箱A-01は廃止済み / 操作: 選び直さずに取り消し、次に箱B-01を選ぶ / 期待: 1回目は拒否、2回目は箱B-01に戻る。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1))
        status.release(conn, item_id, 'sold', SALE)
        retire_container(conn, places.box_a1)
        assert status.retired_destination(conn, item_id) == '書斎 › 本棚 › 上段 › 箱 [A-01]'
        with pytest.raises(ValidationError) as error:
            status.undo_release(conn, item_id)
        assert '廃止済み' in error.value.errors['container_id']
        status.undo_release(conn, item_id, places.box_b1)
        assert items.get_item(conn, item_id).container_id == places.box_b1
        latest: Event = events.list_events(conn, 'item', item_id)[0]
        assert '箱 [A-01]' in latest.memo
        assert '押入れ › 箱 [B-01]' in latest.memo

    def test_give_back_to_retired_destination_needs_new_place(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 箱A-01を戻し先とする貸出中の書籍、箱A-01は廃止済み / 操作: 廃止済みの場所を選んで返却する / 期待: 拒否される。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1))
        status.lend(conn, item_id, LEND)
        retire_container(conn, places.box_a1)
        with pytest.raises(ValidationError):
            status.give_back(conn, item_id, places.box_a1)
        status.give_back(conn, item_id, places.box_a2)
        assert items.get_item(conn, item_id).container_id == places.box_a2


class TestDeleteAndRestore:
    def test_delete_requires_reason(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 書籍 / 操作: 理由を空にして削除する / 期待: 拒否され、削除されない。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1))
        with pytest.raises(ValidationError) as error:
            status.delete_item(conn, item_id, '  ')
        assert set(error.value.errors) == {'reason'}
        assert not items.get_item(conn, item_id).deleted

    def test_deleted_item_is_hidden_unless_requested(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 書籍 / 操作: 理由を付けて削除する / 期待: 通常の検索から消え、「削除済みも表示」で見つかり、理由が履歴に残る。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1))
        status.delete_item(conn, item_id, '二重に登録した')
        assert _found(conn, 'リーダブル') == []
        assert _found(conn, 'リーダブル', include_deleted=True) == [item_id]
        assert items.count_items(conn, SearchQuery(include_deleted=True)) == 1
        latest: Event = events.list_events(conn, 'item', item_id)[0]
        assert (latest.kind, latest.memo) == ('delete', '二重に登録した')
        assert items.get_item(conn, item_id).deleted_at is not None

    def test_deleted_item_rejects_other_operations(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 削除済みの書籍 / 操作: 取り出す・売却・再削除 / 期待: すべて拒否される。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1))
        status.delete_item(conn, item_id, '誤登録')
        with pytest.raises(ValidationError):
            status.take_out(conn, item_id)
        with pytest.raises(ValidationError):
            status.release(conn, item_id, 'sold', SALE)
        with pytest.raises(ValidationError):
            status.delete_item(conn, item_id, '誤登録')

    def test_restore_brings_item_back(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 貸出中で削除した書籍 / 操作: 復元する / 期待: 貸出中のまま検索に戻り、復元の履歴が残る。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1))
        status.lend(conn, item_id, LEND)
        status.delete_item(conn, item_id, '誤登録')
        status.restore_item(conn, item_id)
        item: ItemDetail = items.get_item(conn, item_id)
        assert (item.deleted, item.deleted_at, item.status, item.container_id) == (False, None, 'lent', places.box_a1)
        assert _found(conn, 'リーダブル') == [item_id]
        assert events.list_events(conn, 'item', item_id)[0].kind == 'restore'

    def test_restore_to_retired_container_needs_new_place(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 箱A-01で削除した書籍、箱A-01は廃止済み / 操作: 箱B-01を選んで復元する / 期待: 箱B-01に保管中として戻る。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1))
        status.delete_item(conn, item_id, '誤登録')
        retire_container(conn, places.box_a1)
        with pytest.raises(ValidationError):
            status.restore_item(conn, item_id)
        status.restore_item(conn, item_id, places.box_b1)
        assert items.get_item(conn, item_id).container_id == places.box_b1

    def test_restore_released_item_ignores_retired_container(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 売却済で削除した書籍、元の箱は廃止済み / 操作: 復元する / 期待: 保管場所を選ばずに売却済のまま戻る。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1))
        status.release(conn, item_id, 'sold', SALE)
        status.delete_item(conn, item_id, '誤登録')
        retire_container(conn, places.box_a1)
        status.restore_item(conn, item_id)
        assert (items.get_item(conn, item_id).status, items.get_item(conn, item_id).deleted) == ('sold', False)

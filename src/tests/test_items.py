"""物品の登録・内容の修正・移動・変更履歴のテスト。"""

import sqlite3
from typing import Any

import pytest

from okiba import catalog, events, forms, items
from okiba.catalog import Field
from okiba.common import ValidationError
from okiba.events import Event
from okiba.items import ItemDetail, ItemInput
from tests.helpers import BOOKS_ID, CABLES_ID, Places, book_input, cable_input, set_status


class TestRegister:
    def test_cable_name_is_generated_from_attributes(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 名称を空にしたケーブル / 操作: 登録する / 期待: 端子と長さから名称が付く。"""
        item_id: int = items.register_item(conn, cable_input(places.box_a1))
        assert items.get_item(conn, item_id).name == 'HDMI - DisplayPort 1.8m'

    def test_cable_name_without_length_and_with_integer_length(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 長さなし・長さ2.0mのケーブル / 操作: 登録する / 期待: 長さを省いた名称、整数は小数点なしの名称になる。"""
        no_length: int = items.register_item(conn, cable_input(places.box_a1, b='USB-C', length=None))
        integer_length: int = items.register_item(conn, cable_input(places.box_a1, length=2.0))
        assert items.get_item(conn, no_length).name == 'HDMI - USB-C'
        assert items.get_item(conn, integer_length).name == 'HDMI - DisplayPort 2m'

    def test_entered_cable_name_is_kept(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 名称を入力したケーブル / 操作: 登録する / 期待: 入力した名称のまま。"""
        item_id: int = items.register_item(conn, cable_input(places.box_a1, name='モニター用'))
        assert items.get_item(conn, item_id).name == 'モニター用'

    def test_missing_common_fields_are_reported_per_field(self, conn: sqlite3.Connection) -> None:
        """前提: カテゴリ・名称・保管場所・数量が未入力 / 操作: 登録する / 期待: 入力欄ごとのエラーで拒否される。"""
        with pytest.raises(ValidationError) as error:
            items.register_item(conn, ItemInput(category_id=None, name='', quantity=0))
        assert set(error.value.errors) == {'category_id', 'name', 'quantity', 'container_id'}

    def test_unknown_container_is_a_validation_error(self, conn: sqlite3.Connection) -> None:
        """前提: 存在しない保管場所 / 操作: 登録する / 期待: 保管場所のエラーになる。"""
        with pytest.raises(ValidationError) as error:
            items.register_item(conn, book_input(9999))
        assert 'container_id' in error.value.errors

    def test_required_template_field_is_enforced(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 書籍の著者を必須に設定 / 操作: 著者なしで登録する / 期待: 著者欄のエラーで拒否される。"""
        author: Field = catalog.fields_for_category(conn, BOOKS_ID)[0]
        catalog.update_field(conn, author.id, label='著者', sort_order=10, required=True, clear_on_copy=False, dedupe=False)
        with pytest.raises(ValidationError) as error:
            items.register_item(conn, book_input(places.box_a1, attributes={}))
        assert error.value.errors == {'attr__author': '著者を入力してください。'}

    def test_required_identifier_field_is_enforced(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 書籍のISBNを必須に設定 / 操作: ISBNなしで登録する / 期待: ISBN欄のエラーで拒否される。"""
        isbn: Field = catalog.fields_for_category(conn, BOOKS_ID)[2]
        catalog.update_field(conn, isbn.id, label='ISBN', sort_order=30, required=True, clear_on_copy=False, dedupe=False)
        with pytest.raises(ValidationError) as error:
            items.register_item(conn, book_input(places.box_a1, isbn=''))
        assert 'attr__isbn' in error.value.errors

    def test_identifiers_are_stored_outside_attributes(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: ISBN付きの書籍 / 操作: 登録する / 期待: ISBNは識別コードとして保存され、属性JSONには入らない。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1))
        item: ItemDetail = items.get_item(conn, item_id)
        assert item.identifiers == {'isbn': '9784873115658'}
        assert 'isbn' not in item.attributes

    def test_serial_number_requires_quantity_one(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: シリアル番号の項目があるカテゴリ / 操作: シリアル番号付きで数量2を登録する / 期待: 数量のエラーで拒否される。"""
        catalog.add_field(conn, CABLES_ID, 'シリアル番号', 'text', identifier_kind='serial')
        with pytest.raises(ValidationError) as error:
            items.register_item(conn, cable_input(places.box_a1, quantity=2, identifiers={'serial': 'SN-1'}))
        assert 'quantity' in error.value.errors
        items.register_item(conn, cable_input(places.box_a1, quantity=1, identifiers={'serial': 'SN-1'}))

    def test_retired_category_and_container_cannot_be_selected(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 廃止済みのカテゴリ・保管場所 / 操作: それらを選んで登録する / 期待: 拒否される。"""
        conn.execute('UPDATE containers SET retired = 1 WHERE id = ?', (places.box_a2,))
        with pytest.raises(ValidationError) as error:
            items.register_item(conn, cable_input(places.box_a2))
        assert 'container_id' in error.value.errors
        conn.execute('UPDATE categories SET retired = 1 WHERE id = ?', (CABLES_ID,))
        with pytest.raises(ValidationError) as error:
            items.register_item(conn, cable_input(places.box_a1))
        assert 'category_id' in error.value.errors

    def test_tags_are_normalized_and_created(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 全角英字と重複を含むタグ / 操作: 登録する / 期待: 正規化して重複なく付き、未登録のタグは作られる。"""
        item_id: int = items.register_item(conn, cable_input(places.box_a1, tag_names=['ＡＢＣ', '予備']))
        items.register_item(conn, cable_input(places.box_a1, tag_names=['ABC']))
        assert items.get_item(conn, item_id).tags == ['ABC', '予備']
        assert conn.execute('SELECT count(*) FROM tags').fetchone()[0] == 2

    def test_create_event_keeps_full_snapshot(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: タグ・ISBN付きの書籍 / 操作: 登録する / 期待: 登録の履歴に、属性・タグ・識別コードを含む完全な内容が残る。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1, tag_names=['技術書']))
        history: list[Event] = events.list_events(conn, 'item', item_id)
        assert [event.kind for event in history] == ['create']
        after: dict[str, Any] = history[0].after or {}
        assert after['attributes'] == {'author': 'Dustin Boswell', 'publisher': 'オライリー・ジャパン'}
        assert after['identifiers'] == [{'kind': 'isbn', 'value': '9784873115658'}]
        assert [tag['name'] for tag in after['tags']] == ['技術書']
        assert (after['status'], after['container_id']) == ('stored', places.box_a1)


class TestUpdate:
    def test_generated_cable_name_follows_attribute_changes(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 自動生成した名称のケーブル / 操作: 長さを変える / 期待: 名称も作り直される。"""
        item_id: int = items.register_item(conn, cable_input(places.box_a1))
        items.update_item(conn, item_id, cable_input(places.box_a1, name='HDMI - DisplayPort 1.8m', length=3))
        assert items.get_item(conn, item_id).name == 'HDMI - DisplayPort 3m'

    def test_manually_edited_cable_name_is_not_overwritten(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 名称を手で直したケーブル / 操作: 長さを変える / 期待: 名称は手で直したまま。"""
        item_id: int = items.register_item(conn, cable_input(places.box_a1, name='モニター用'))
        items.update_item(conn, item_id, cable_input(places.box_a1, name='モニター用', length=3))
        assert items.get_item(conn, item_id).name == 'モニター用'

    def test_name_changed_in_same_edit_wins(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 自動生成した名称のケーブル / 操作: 名称と長さを同時に変える / 期待: 入力した名称になる。"""
        item_id: int = items.register_item(conn, cable_input(places.box_a1))
        items.update_item(conn, item_id, cable_input(places.box_a1, name='予備ケーブル', length=3))
        assert items.get_item(conn, item_id).name == '予備ケーブル'

    def test_update_records_before_and_after(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 書籍 / 操作: 備考と数量を変える / 期待: 修正の履歴に変更前後の完全な内容が残る。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1))
        items.update_item(conn, item_id, book_input(places.box_a1, note='2冊目', quantity=2))
        latest: Event = events.list_events(conn, 'item', item_id)[0]
        assert latest.kind == 'update'
        assert (latest.before or {})['note'] == ''
        assert ((latest.after or {})['note'], (latest.after or {})['quantity']) == ('2冊目', 2)

    def test_update_without_changes_records_nothing(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 書籍 / 操作: 同じ内容で保存する / 期待: 修正の履歴は増えない。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1))
        items.update_item(conn, item_id, book_input(places.box_a1))
        assert [event.kind for event in events.list_events(conn, 'item', item_id)] == ['create']

    def test_newly_required_field_blocks_edit_but_not_move(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 登録後に必須になった項目が空の書籍 / 操作: 内容の修正と移動を行う / 期待: 修正は拒否、移動はできる。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1, attributes={}))
        author: Field = catalog.fields_for_category(conn, BOOKS_ID)[0]
        catalog.update_field(conn, author.id, label='著者', sort_order=10, required=True, clear_on_copy=False, dedupe=False)
        with pytest.raises(ValidationError):
            items.update_item(conn, item_id, book_input(places.box_a1, attributes={}, note='メモ'))
        items.move_item(conn, item_id, places.box_b1)
        assert items.get_item(conn, item_id).container_id == places.box_b1

    def test_category_change_keeps_unmatched_attributes(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 書籍 / 操作: カテゴリをケーブル類に変えて保存する / 期待: 著者・出版社はテンプレート外の項目として残り、ISBNも消えない。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1))
        item: ItemDetail = items.get_item(conn, item_id)
        book_fields: list[Field] = catalog.fields_for_category(conn, BOOKS_ID)
        cable_fields: list[Field] = catalog.fields_for_category(conn, CABLES_ID)
        form: forms.ItemForm = forms.form_from_item(item, BOOKS_ID, None, book_fields)
        form.major_id = CABLES_ID
        forms.move_unmatched_to_extras(form, book_fields, cable_fields)
        data: ItemInput
        errors: dict[str, str]
        data, errors = forms.form_to_input(form, cable_fields, item.attributes, item.identifiers)
        assert errors == {}
        items.update_item(conn, item_id, data)
        updated: ItemDetail = items.get_item(conn, item_id)
        assert updated.category_id == CABLES_ID
        assert updated.attributes == {'author': 'Dustin Boswell', 'publisher': 'オライリー・ジャパン'}
        assert updated.identifiers == {'isbn': '9784873115658'}
        assert updated.name == 'リーダブルコード'


class TestMove:
    def test_move_changes_container_and_records_event(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 箱A-01の書籍 / 操作: 箱B-01へ移動する / 期待: 保管場所が変わり、移動の履歴が残る。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1))
        items.move_item(conn, item_id, places.box_b1)
        latest: Event = events.list_events(conn, 'item', item_id)[0]
        assert latest.kind == 'move'
        assert ((latest.before or {})['container_id'], (latest.after or {})['container_id']) == (places.box_a1, places.box_b1)

    def test_moving_lent_item_changes_return_destination(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 箱A-01を戻し先とする貸出中の書籍 / 操作: 箱B-01へ移動する / 期待: 状態はそのままで戻し先が変わる。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1))
        set_status(conn, item_id, 'lent')
        items.move_item(conn, item_id, places.box_b1)
        item: ItemDetail = items.get_item(conn, item_id)
        assert (item.status, item.container_id, item.is_return_target) == ('lent', places.box_b1, True)

    def test_released_item_cannot_move(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 売却済の書籍 / 操作: 移動する / 期待: 拒否される。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1))
        set_status(conn, item_id, 'sold')
        with pytest.raises(ValidationError):
            items.move_item(conn, item_id, places.box_b1)

    def test_same_container_move_records_nothing(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 箱A-01の書籍 / 操作: 箱A-01へ移動する / 期待: 履歴は増えない。"""
        item_id: int = items.register_item(conn, book_input(places.box_a1))
        items.move_item(conn, item_id, places.box_a1)
        assert len(events.list_events(conn, 'item', item_id)) == 1

"""入力フォームの値の解釈・複製登録・テンプレート外の項目のテスト。"""

import sqlite3
from typing import Any

import pytest

from okiba import catalog, items
from okiba.catalog import Field
from okiba.forms import ItemForm, copy_form, form_from_item, form_from_mapping, form_to_input
from okiba.items import ItemDetail, ItemInput
from tests.helpers import BOOKS_ID, CABLES_ID, Places, book_input, cable_input


def _fields_of_each_type(conn: sqlite3.Connection) -> list[Field]:
    category_id: int = catalog.create_category(conn, '型の確認')
    catalog.add_field(conn, category_id, '数', 'number', key='count')
    catalog.add_field(conn, category_id, '長さ', 'measure', unit_kind='length', key='size')
    catalog.add_field(conn, category_id, '購入日', 'date', key='bought')
    catalog.add_field(conn, category_id, '動作品', 'bool', key='works')
    return catalog.fields_for_category(conn, category_id)


class TestParse:
    def test_values_are_converted_by_field_type(self, conn: sqlite3.Connection) -> None:
        """前提: 数値・数値＋単位・日付・真偽値の項目 / 操作: 正しい値を入力する / 期待: 型付きの値に変換される。"""
        fields: list[Field] = _fields_of_each_type(conn)
        form: ItemForm = ItemForm(values={'count': '3', 'size': '1.5', 'size__unit': 'cm', 'bought': '2026-10-01', 'works': 'true'})
        data: ItemInput
        errors: dict[str, str]
        data, errors = form_to_input(form, fields)
        assert errors == {}
        assert data.attributes == {'count': 3, 'size': {'value': 1.5, 'unit': 'cm'}, 'bought': '2026-10-01', 'works': True}

    @pytest.mark.parametrize(
        ('values', 'error_key'),
        [
            ({'count': 'たくさん'}, 'attr__count'),
            ({'size': '-1', 'size__unit': 'cm'}, 'attr__size'),
            ({'size': '1', 'size__unit': 'inch'}, 'attr__size'),
            ({'bought': '2026-13-01'}, 'attr__bought'),
            ({'works': 'maybe'}, 'attr__works'),
        ],
        ids=['数値でない', '負の長さ', '未対応の単位', '存在しない日付', '真偽値でない'],
    )
    def test_invalid_values_are_reported(self, conn: sqlite3.Connection, values: dict[str, str], error_key: str) -> None:
        """前提: 型ごとの項目 / 操作: 型に合わない値を入力する / 期待: 該当欄のエラーになる。"""
        fields: list[Field] = _fields_of_each_type(conn)
        errors: dict[str, str]
        _, errors = form_to_input(ItemForm(values=values), fields)
        assert error_key in errors

    def test_empty_values_are_omitted(self, conn: sqlite3.Connection) -> None:
        """前提: 型ごとの項目 / 操作: すべて空欄にする / 期待: 属性に何も入らず、エラーもない。"""
        fields: list[Field] = _fields_of_each_type(conn)
        data: ItemInput
        errors: dict[str, str]
        data, errors = form_to_input(ItemForm(values={'count': ' ', 'size': '', 'size__unit': 'm'}), fields)
        assert (data.attributes, errors) == ({}, {})

    def test_non_integer_quantity_is_reported(self, conn: sqlite3.Connection) -> None:
        """前提: なし / 操作: 数量に小数を入力する / 期待: 数量のエラーになる。"""
        errors: dict[str, str]
        _, errors = form_to_input(ItemForm(quantity='1.5'), [])
        assert 'quantity' in errors

    def test_submitted_mapping_is_read_into_form(self) -> None:
        """前提: 送信されたフォームの値 / 操作: フォームの状態にする / 期待: 属性・テンプレート外・選択値が振り分けられ、ファイルは無視される。"""
        form: ItemForm = form_from_mapping(
            {'major_id': '2', 'minor_id': '', 'name': 'x', 'attr__connector_a': 'HDMI', 'extra__memo': 'メモ', 'photos': object()}
        )
        assert (form.major_id, form.minor_id, form.category_id) == (2, None, 2)
        assert (form.values, form.extras) == ({'connector_a': 'HDMI'}, {'memo': 'メモ'})


class TestExtras:
    def _item_with_extras(self, conn: sqlite3.Connection, places: Places) -> ItemDetail:
        attributes: dict[str, Any] = {'connector_a': 'HDMI', 'old_length': {'value': 2, 'unit': 'm'}, 'x:色': '黒'}
        item_id: int = items.register_item(conn, cable_input(places.box_a1, attributes=attributes))
        return items.get_item(conn, item_id)

    def test_unchanged_extra_keeps_typed_value(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 数値＋単位のテンプレート外の項目 / 操作: 表示どおりのまま保存する / 期待: 型付きの値が保たれる。"""
        item: ItemDetail = self._item_with_extras(conn, places)
        fields: list[Field] = catalog.fields_for_category(conn, CABLES_ID)
        form: ItemForm = form_from_item(item, CABLES_ID, None, fields)
        assert form.extras == {'old_length': '2m', 'x:色': '黒'}
        data: ItemInput
        data, _ = form_to_input(form, fields, item.attributes, item.identifiers)
        assert data.attributes['old_length'] == {'value': 2, 'unit': 'm'}

    def test_edited_extra_becomes_text_and_cleared_extra_is_removed(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: テンプレート外の項目2つ / 操作: 1つを書き換え、1つを空にして保存する / 期待: 書き換えた値は文字列で残り、空にした項目は消える。"""
        item: ItemDetail = self._item_with_extras(conn, places)
        fields: list[Field] = catalog.fields_for_category(conn, CABLES_ID)
        form: ItemForm = form_from_item(item, CABLES_ID, None, fields)
        form.extras = {'old_length': '3m', 'x:色': ''}
        data: ItemInput
        data, _ = form_to_input(form, fields, item.attributes, item.identifiers)
        assert data.attributes == {'connector_a': 'HDMI', 'old_length': '3m'}


class TestCopy:
    def test_copy_keeps_product_codes_and_clears_serial_and_flagged_fields(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 型番・シリアル番号・複製時クリアの項目を持つ登録 / 操作: 複製登録のフォームを作る / 期待: 型番だけが残る。"""
        catalog.add_field(conn, CABLES_ID, '型番', 'text', identifier_kind='model', key='model_no')
        catalog.add_field(conn, CABLES_ID, 'シリアル番号', 'text', identifier_kind='serial', key='serial_no')
        catalog.add_field(conn, CABLES_ID, '購入店', 'text', clear_on_copy=True, key='shop')
        source: int = items.register_item(
            conn,
            cable_input(
                places.box_a1,
                identifiers={'model': 'HD-01', 'serial': 'SN-1'},
                tag_names=['予備'],
                note='メモ',
                attributes={
                    'connector_a': 'HDMI',
                    'connector_b': 'DisplayPort',
                    'shop': '家電店',
                },
            ),
        )
        fields: list[Field] = catalog.fields_for_category(conn, CABLES_ID)
        form: ItemForm = copy_form(items.get_item(conn, source), CABLES_ID, None, fields)
        assert form.values == {'connector_a': 'HDMI', 'connector_b': 'DisplayPort', 'model_no': 'HD-01'}
        assert (form.container_id, form.quantity, form.tags, form.note, form.name) == (str(places.box_a1), '1', '予備', 'メモ', 'HDMI - DisplayPort')

    def test_copy_of_book_keeps_isbn(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: ISBN付きの書籍 / 操作: 複製登録のフォームを作る / 期待: 製品共通コードのISBNは引き継ぐ。"""
        source: int = items.register_item(conn, book_input(places.box_a1))
        form: ItemForm = copy_form(items.get_item(conn, source), BOOKS_ID, None, catalog.fields_for_category(conn, BOOKS_ID))
        assert form.values['isbn'] == '9784873115658'

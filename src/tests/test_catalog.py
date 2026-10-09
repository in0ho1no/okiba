"""カテゴリとテンプレートのテスト。"""

import sqlite3

import pytest

from okiba import catalog, items
from okiba.catalog import Field
from okiba.common import ValidationError
from tests.helpers import BOOKS_ID, CABLES_ID, Places, cable_input


class TestCategories:
    def test_categories_are_limited_to_two_levels(self, conn: sqlite3.Connection) -> None:
        """前提: 大カテゴリ「ケーブル類」 / 操作: 小カテゴリの下にさらにカテゴリを作る / 期待: 拒否される。"""
        minor: int = catalog.create_category(conn, '映像', CABLES_ID)
        with pytest.raises(ValidationError) as error:
            catalog.create_category(conn, '細分類', minor)
        assert 'parent_id' in error.value.errors

    def test_same_name_is_rejected_only_under_same_parent(self, conn: sqlite3.Connection) -> None:
        """前提: 書籍・ケーブル類 / 操作: 同じ親に同名、別の親に同名のカテゴリを作る / 期待: 同じ親だけ拒否される。"""
        catalog.create_category(conn, 'その他', BOOKS_ID)
        catalog.create_category(conn, 'その他', CABLES_ID)
        with pytest.raises(ValidationError):
            catalog.create_category(conn, 'その他', BOOKS_ID)
        with pytest.raises(ValidationError):
            catalog.create_category(conn, '書籍')

    def test_rename_rejects_empty_and_duplicate_names(self, conn: sqlite3.Connection) -> None:
        """前提: 書籍・ケーブル類 / 操作: 空の名前、既存の名前に変更する / 期待: どちらも拒否され、元の名前のまま。"""
        with pytest.raises(ValidationError):
            catalog.rename_category(conn, BOOKS_ID, ' ')
        with pytest.raises(ValidationError):
            catalog.rename_category(conn, BOOKS_ID, 'ケーブル類')
        assert catalog.get_category(conn, BOOKS_ID).name == '書籍'

    def test_book_name_label_is_title_and_inherited_by_minor(self, conn: sqlite3.Connection) -> None:
        """前提: 書籍の名称欄は「タイトル」 / 操作: 書籍の小カテゴリの名称欄の表示名を得る / 期待: 「タイトル」を引き継ぐ。"""
        minor: int = catalog.create_category(conn, '技術書', BOOKS_ID)
        assert catalog.name_label(conn, minor) == 'タイトル'
        assert catalog.name_label(conn, CABLES_ID) == '名称'


class TestTemplate:
    def test_initial_templates_match_spec(self, conn: sqlite3.Connection) -> None:
        """前提: 初期状態 / 操作: 書籍とケーブル類のテンプレートを得る / 期待: 仕様どおりの項目・型・識別子・重複判定設定。"""
        books: list[Field] = catalog.fields_for_category(conn, BOOKS_ID)
        assert [(f.key, f.label, f.identifier_kind) for f in books] == [
            ('author', '著者', None),
            ('publisher', '出版社', None),
            ('isbn', 'ISBN', 'isbn'),
        ]
        cables: list[Field] = catalog.fields_for_category(conn, CABLES_ID)
        assert [(f.key, f.field_type, f.dedupe) for f in cables] == [
            ('connector_a', 'suggest', True),
            ('connector_b', 'suggest', True),
            ('length', 'measure', False),
        ]
        assert cables[2].unit_kind == 'length'

    def test_minor_template_merges_major_fields_in_sort_order(self, conn: sqlite3.Connection) -> None:
        """前提: ケーブル類の小カテゴリ / 操作: 小カテゴリに表示順15の項目を追加する / 期待: 大カテゴリの項目と表示順で合成される。"""
        minor: int = catalog.create_category(conn, '映像', CABLES_ID)
        catalog.add_field(conn, minor, '規格', 'text', sort_order=15)
        keys: list[str] = [f.key for f in catalog.fields_for_category(conn, minor)]
        assert keys == ['connector_a', 'field_1', 'connector_b', 'length']

    def test_auto_keys_are_sequential_and_never_reused(self, conn: sqlite3.Connection) -> None:
        """前提: 項目を2つ追加し、1つ目を廃止 / 操作: さらに項目を追加する / 期待: 廃止した内部キーは再利用されない。"""
        first: int = catalog.add_field(conn, CABLES_ID, '色', 'text')
        catalog.add_field(conn, CABLES_ID, '規格', 'text')
        conn.execute('UPDATE category_fields SET retired = 1 WHERE id = ?', (first,))
        third: int = catalog.add_field(conn, CABLES_ID, '備品番号', 'text')
        assert catalog.get_field(conn, third).key == 'field_3'
        with pytest.raises(ValidationError) as error:
            catalog.add_field(conn, CABLES_ID, '色', 'text', key='field_1')
        assert 'key' in error.value.errors

    def test_key_must_be_unique_across_major_and_minor(self, conn: sqlite3.Connection) -> None:
        """前提: 大カテゴリに connector_a、小カテゴリに color / 操作: 互いに同じ内部キーを追加する / 期待: どちら向きも拒否される。"""
        minor: int = catalog.create_category(conn, '映像', CABLES_ID)
        catalog.add_field(conn, minor, '色', 'text', key='color')
        with pytest.raises(ValidationError):
            catalog.add_field(conn, minor, '端子', 'text', key='connector_a')
        with pytest.raises(ValidationError):
            catalog.add_field(conn, CABLES_ID, '色', 'text', key='color')

    @pytest.mark.parametrize(
        ('field_type', 'options', 'error_key'),
        [
            ('measure', {}, 'unit_kind'),
            ('number', {'identifier_kind': 'jan'}, 'identifier_kind'),
            ('text', {'identifier_kind': 'isbn'}, 'identifier_kind'),
            ('unknown', {}, 'field_type'),
            ('text', {'key': 'Bad-Key'}, 'key'),
        ],
        ids=['単位の種類なしの数値＋単位', 'テキスト以外の識別コード', '同じ種類の識別コードが既にある', '不明な型', '内部キーの形式違反'],
    )
    def test_invalid_field_definitions_are_rejected(self, conn: sqlite3.Connection, field_type: str, options: dict[str, str], error_key: str) -> None:
        """前提: 書籍（ISBN項目あり） / 操作: 不正な項目定義を追加する / 期待: 該当欄のエラーで拒否される。"""
        with pytest.raises(ValidationError) as error:
            catalog.add_field(conn, BOOKS_ID, '項目', field_type, **options)  # type: ignore[arg-type]
        assert error_key in error.value.errors

    def test_update_changes_only_mutable_settings(self, conn: sqlite3.Connection) -> None:
        """前提: ケーブル類の端子A / 操作: 表示名・表示順・必須などを更新する / 期待: 変更でき、内部キーと型は変わらない。"""
        field_id: int = catalog.fields_for_category(conn, CABLES_ID)[0].id
        catalog.update_field(conn, field_id, label='端子（一方）', sort_order=5, required=True, clear_on_copy=True, dedupe=False)
        updated: Field = catalog.get_field(conn, field_id)
        assert (updated.label, updated.sort_order, updated.required, updated.clear_on_copy, updated.dedupe) == ('端子（一方）', 5, True, True, False)
        assert (updated.key, updated.field_type) == ('connector_a', 'suggest')

    def test_suggestions_list_previous_values(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 端子AにHDMIとUSB-Cのケーブル / 操作: 端子Aの候補を得る / 期待: 過去の入力値が重複なく並ぶ。"""
        items.register_item(conn, cable_input(places.box_a1, a='HDMI'))
        items.register_item(conn, cable_input(places.box_a1, a='HDMI'))
        items.register_item(conn, cable_input(places.box_a1, a='USB-C'))
        assert catalog.suggestions(conn, 'connector_a') == ['HDMI', 'USB-C']

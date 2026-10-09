"""検索（全文検索・絞り込み・既定の表示範囲）のテスト。"""

import sqlite3

import pytest

from okiba import catalog, items
from okiba.items import SearchQuery
from tests.helpers import BOOKS_ID, CABLES_ID, Places, book_input, cable_input, set_status


def _names(conn: sqlite3.Connection, **query: object) -> list[str]:
    return sorted(summary.name for summary in items.search_items(conn, SearchQuery(**query)))  # type: ignore[arg-type]


@pytest.fixture
def stock(conn: sqlite3.Connection, places: Places) -> dict[str, int]:
    """書籍2冊（箱A-01・箱B-01）とケーブル2本（箱A-02）。"""
    return {
        'readable': items.register_item(conn, book_input(places.box_a1, note='第2版を購入予定', tag_names=['技術書'])),
        'novel': items.register_item(conn, book_input(places.box_b1, title='吾輩は猫である', isbn='9784101010014')),
        'hdmi': items.register_item(conn, cable_input(places.box_a2)),
        'usb': items.register_item(conn, cable_input(places.box_a2, a='USB-A', b='USB-C', length=50, unit='cm')),
    }


class TestFullText:
    @pytest.mark.parametrize(
        ('text', 'expected'),
        [
            ('リーダブル', ['リーダブルコード']),
            ('猫', ['吾輩は猫である']),
            ('コー', ['リーダブルコード']),
            ('ｈｄｍｉ', ['HDMI - DisplayPort 1.8m']),
            ('displayport', ['HDMI - DisplayPort 1.8m']),
            ('9784101010014', ['吾輩は猫である']),
            ('第2版', ['リーダブルコード']),
            ('オライリー', ['リーダブルコード', '吾輩は猫である']),
            ('50cm', ['USB-A - USB-C 50cm']),
            ('USB 50', ['USB-A - USB-C 50cm']),
            ('存在しない語', []),
        ],
        ids=[
            '3文字以上の日本語',
            '1文字',
            '2文字',
            '全角英字',
            '大文字小文字の違い',
            '識別コード',
            '備考',
            '属性',
            '数値＋単位',
            '複数語のAND',
            '該当なし',
        ],
    )
    def test_text_matches_name_note_attributes_and_identifiers(
        self, conn: sqlite3.Connection, stock: dict[str, int], text: str, expected: list[str]
    ) -> None:
        """前提: 書籍2冊とケーブル2本 / 操作: キーワードで検索する / 期待: 名称・備考・属性・識別コードから該当する物が見つかる。"""
        assert _names(conn, text=text) == expected

    def test_like_wildcards_are_literal(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 名称に「%」を含む物と含まない物 / 操作: 「%」で検索する / 期待: 「%」を含む物だけが見つかる。"""
        items.register_item(conn, book_input(places.box_a1, title='100%入門', isbn=''))
        items.register_item(conn, book_input(places.box_a1, title='入門', isbn=''))
        assert _names(conn, text='%') == ['100%入門']

    def test_index_follows_updates(self, conn: sqlite3.Connection, places: Places, stock: dict[str, int]) -> None:
        """前提: 書籍 / 操作: 備考を書き換える / 期待: 新しい備考で見つかり、古い備考では見つからない。"""
        items.update_item(conn, stock['readable'], book_input(places.box_a1, note='貸出予定', tag_names=['技術書']))
        assert _names(conn, text='貸出予定') == ['リーダブルコード']
        assert _names(conn, text='第2版') == []


class TestFilters:
    def test_major_category_includes_minor_categories(self, conn: sqlite3.Connection, places: Places, stock: dict[str, int]) -> None:
        """前提: 書籍の小カテゴリ「小説」の登録 / 操作: 大カテゴリ・小カテゴリで絞り込む / 期待: 大カテゴリは配下を含み、小カテゴリはその分だけ。"""
        novels: int = catalog.create_category(conn, '小説', BOOKS_ID)
        items.register_item(conn, book_input(places.box_a1, title='坊っちゃん', isbn='', category_id=novels))
        assert _names(conn, category_id=BOOKS_ID) == ['リーダブルコード', '吾輩は猫である', '坊っちゃん']
        assert _names(conn, category_id=novels) == ['坊っちゃん']
        assert len(_names(conn, category_id=CABLES_ID)) == 2

    def test_container_filter_includes_nested_containers(self, conn: sqlite3.Connection, places: Places, stock: dict[str, int]) -> None:
        """前提: 書斎配下の箱A-01・A-02と押入れの箱B-01 / 操作: 書斎で絞り込む / 期待: 配下の箱の中身が含まれる。"""
        assert _names(conn, container_id=places.study) == ['HDMI - DisplayPort 1.8m', 'USB-A - USB-C 50cm', 'リーダブルコード']
        assert _names(conn, container_id=places.box_b1) == ['吾輩は猫である']

    def test_tag_filter(self, conn: sqlite3.Connection, stock: dict[str, int]) -> None:
        """前提: 「技術書」タグの書籍 / 操作: タグで絞り込む / 期待: タグの付いた物だけ。"""
        tag_id: int = conn.execute("SELECT id FROM tags WHERE name = '技術書'").fetchone()[0]
        assert _names(conn, tag_id=tag_id) == ['リーダブルコード']

    def test_default_shows_only_owned_and_valid_items(self, conn: sqlite3.Connection, places: Places, stock: dict[str, int]) -> None:
        """前提: 売却済・貸出中・削除済み・統合済みの登録 / 操作: 既定の条件で検索する / 期待: 所持中で有効な登録だけ。売却済は絞り込みで出る。"""
        set_status(conn, stock['novel'], 'sold')
        set_status(conn, stock['readable'], 'lent')
        conn.execute('UPDATE items SET deleted = 1 WHERE id = ?', (stock['usb'],))
        merged: int = items.register_item(conn, cable_input(places.box_a2))
        conn.execute('UPDATE items SET merged_into_id = ? WHERE id = ?', (stock['hdmi'], merged))
        assert _names(conn) == ['HDMI - DisplayPort 1.8m', 'リーダブルコード']
        assert _names(conn, statuses=('sold', 'disposed')) == ['吾輩は猫である']

    def test_count_matches_search_for_every_filter(self, conn: sqlite3.Connection, places: Places, stock: dict[str, int]) -> None:
        """前提: 階層・状態・削除・統合の異なる物品 / 操作: 各条件で検索と件数取得 / 期待: 総件数と結果件数が一致する。"""
        novels: int = catalog.create_category(conn, '小説', BOOKS_ID)
        items.register_item(conn, book_input(places.box_a1, title='新しい小説', isbn='', category_id=novels))
        set_status(conn, stock['novel'], 'sold')
        set_status(conn, stock['readable'], 'lent')
        conn.execute('UPDATE items SET deleted = 1 WHERE id = ?', (stock['usb'],))
        merged: int = items.register_item(conn, cable_input(places.box_a2))
        conn.execute('UPDATE items SET merged_into_id = ? WHERE id = ?', (stock['hdmi'], merged))
        tag_id: int = conn.execute('SELECT id FROM tags WHERE name = ?', ('技術書',)).fetchone()[0]

        queries: list[SearchQuery] = [
            SearchQuery(),
            SearchQuery(statuses=()),
            SearchQuery(statuses=('sold',)),
            SearchQuery(statuses=('stored', 'lent')),
            SearchQuery(category_id=BOOKS_ID),
            SearchQuery(category_id=novels),
            SearchQuery(category_id=CABLES_ID),
            SearchQuery(container_id=places.study),
            SearchQuery(container_id=places.box_b1),
            SearchQuery(tag_id=tag_id),
            SearchQuery(text='リーダブル'),
            SearchQuery(text='猫'),
            SearchQuery(text='HDMI 1.8m'),
            SearchQuery(text='存在しない'),
            SearchQuery(text='リーダブル', category_id=BOOKS_ID, container_id=places.study, tag_id=tag_id, statuses=('lent',)),
        ]
        for query in queries:
            result_count: int = len(items.search_items(conn, query, limit=100))
            assert items.count_items(conn, query) == result_count, query

    def test_location_marks_return_destination(self, conn: sqlite3.Connection, places: Places, stock: dict[str, int]) -> None:
        """前提: 貸出中の書籍 / 操作: 検索する / 期待: 所在が戻し先として扱われ、保管場所の階層で表示される。"""
        set_status(conn, stock['readable'], 'lent')
        summary: items.ItemSummary = next(s for s in items.search_items(conn, SearchQuery(text='リーダブル')))
        assert summary.is_return_target
        assert summary.location == '書斎 › 本棚 › 上段 › 箱 [A-01]'

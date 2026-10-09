"""画面のHTTPシナリオのテスト。

依存パッケージ（FastAPI・Starlette・Jinja2・python-multipart）の更新で画面の動作が壊れていないことを確認する。
"""

import re
import sqlite3
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from okiba import items
from okiba.backup import WriteGate
from okiba.config import Settings
from okiba.web.app import create_app
from okiba.web.csrf import COOKIE_NAME as CSRF_COOKIE
from okiba.web.csrf import FIELD_NAME as CSRF_FIELD
from tests.helpers import BOOKS_ID, CABLES_ID, Places, book_input, image_bytes


def _post(client: TestClient, url: str, data: dict[str, str], files: list[tuple[str, tuple[str, bytes, str]]] | None = None) -> Response:
    """画面のフォームと同じく、Cookieで受け取ったCSRF対策のトークンを付けて送信する。"""
    response: Response = client.post(url, data={**data, CSRF_FIELD: client.cookies[CSRF_COOKIE]}, files=files)
    return response


@pytest.fixture
def box(client: TestClient) -> str:
    """書斎 › 箱A-01 を作り、箱のIDを返す。"""
    _post(client, '/containers', {'name': '書斎', 'kind_id': '1', 'parent_id': '', 'label': ''})
    response: Response = _post(client, '/containers', {'name': '箱', 'kind_id': '4', 'parent_id': '1', 'label': 'A-01'})
    match: re.Match[str] | None = re.search(r'/containers/(\d+)', str(response.url))
    assert match is not None
    return match.group(1)


def _cable_form(box: str, **overrides: str) -> dict[str, str]:
    form: dict[str, str] = {
        'major_id': str(CABLES_ID),
        'minor_id': '',
        'attr__connector_a': 'HDMI',
        'attr__connector_b': 'DisplayPort',
        'attr__length': '1.8',
        'attr__length__unit': 'm',
        'name': '',
        'container_id': box,
        'quantity': '1',
        'tags': '予備',
        'note': '',
        'action': 'save',
    }
    form.update(overrides)
    return form


def _item_id(response: Response) -> str:
    match: re.Match[str] | None = re.search(r'/items/(\d+)', str(response.url))
    assert match is not None
    return match.group(1)


class TestPages:
    @pytest.mark.parametrize('url', ['/', '/items/new', '/manage', '/categories', f'/categories/{BOOKS_ID}', '/tags', '/containers', '/backup'])
    def test_pages_render(self, client: TestClient, url: str) -> None:
        """前提: 初期状態 / 操作: 各画面を開く / 期待: 表示できる。"""
        assert client.get(url).status_code == 200

    @pytest.mark.parametrize('url', ['/static/style.css', '/static/app.js'])
    def test_static_files_are_served(self, client: TestClient, url: str) -> None:
        """前提: なし / 操作: CSS・JavaScriptを取得する / 期待: 配信される。"""
        assert client.get(url).status_code == 200

    @pytest.mark.parametrize('url', ['/items/999', '/containers/999', '/categories/999'])
    def test_missing_data_shows_not_found(self, client: TestClient, url: str) -> None:
        """前提: 存在しないID / 操作: 画面を開く / 期待: 「見つかりません」の画面になる。"""
        response: Response = client.get(url)
        assert response.status_code == 404
        assert '見つかりません' in response.text


class TestRegisterFlow:
    def test_fields_appear_step_by_step(self, client: TestClient) -> None:
        """前提: なし / 操作: 登録画面を開き、大カテゴリを選ぶ / 期待: 選ぶまでは共通項目が出ず、選ぶとテンプレートの項目と共通項目が出る。"""
        assert '共通項目' not in client.get('/items/new').text
        response: Response = _post(client, '/items/new', {'major_id': str(CABLES_ID), 'action': 'refresh'})
        assert '共通項目' in response.text
        assert '端子A' in response.text
        assert '空欄の場合は端子と長さから自動で付けます' in response.text

    def test_register_cable_and_open_detail(self, client: TestClient, box: str) -> None:
        """前提: 箱A-01 / 操作: 名称を空にしてケーブルを登録する / 期待: 詳細画面に自動生成の名称と保管場所が出る。"""
        response: Response = _post(client, '/items/new', _cable_form(box))
        assert response.status_code == 200
        assert '登録しました' in response.text
        assert '<h1>HDMI - DisplayPort 1.8m</h1>' in response.text
        assert '書斎 › 箱 [A-01]' in response.text

    def test_validation_error_keeps_entered_values(self, client: TestClient, box: str) -> None:
        """前提: なし / 操作: 保管場所を選ばず、数量に文字を入れて登録する / 期待: エラーが表示され、入力した端子は残る。"""
        response: Response = _post(client, '/items/new', _cable_form('', quantity='abc'))
        assert response.status_code == 422
        assert '保管場所を選んでください' in response.text
        assert '数量は1以上の整数で入力してください' in response.text
        assert 'value="HDMI"' in response.text

    def test_register_and_continue_opens_prefilled_copy(self, client: TestClient, box: str) -> None:
        """前提: 箱A-01 / 操作: 「登録して同じ内容で続ける」 / 期待: 同じ内容が入った登録画面に戻る。"""
        response: Response = _post(client, '/items/new', _cable_form(box, action='save_continue'))
        assert '/items/new' in str(response.url)
        assert '登録しました' in response.text
        assert 'value="DisplayPort"' in response.text
        assert 'value="予備"' in response.text

    def test_category_and_field_can_be_added_during_registration(self, client: TestClient, box: str) -> None:
        """前提: なし / 操作: 新しい大カテゴリ・小カテゴリと項目を作る / 期待: 作成したカテゴリが選ばれ、追加した項目の入力欄が出る。"""
        response: Response = _post(client, '/items/new', {'new_major_name': '文具', 'action': 'add_major'})
        assert re.search(r'<option value="(\d+)" selected>文具</option>', response.text)
        major_id: str = re.findall(r'<option value="(\d+)" selected>文具</option>', response.text)[0]
        response = _post(client, '/items/new', {'major_id': major_id, 'new_minor_name': 'ペン', 'action': 'add_minor'})
        minor_id: str = re.findall(r'<option value="(\d+)" selected>ペン</option>', response.text)[0]
        response = _post(
            client,
            '/items/new',
            {
                'major_id': major_id,
                'minor_id': minor_id,
                'name': '黒ペン',
                'new_field_label': 'インク色',
                'new_field_type': 'text',
                'new_field_save': 'on',
                'action': 'add_field',
            },
        )
        assert 'インク色' in response.text
        assert 'value="黒ペン"' in response.text
        response = _post(
            client,
            '/items/new',
            {
                'major_id': major_id,
                'minor_id': minor_id,
                'name': '黒ペン',
                'attr__field_1': '黒',
                'container_id': box,
                'quantity': '3',
                'action': 'save',
            },
        )
        assert '文具 › ペン' in response.text
        assert '<dt>インク色</dt><dd>黒</dd>' in response.text

    def test_field_not_saved_to_template_becomes_extra(self, client: TestClient, box: str) -> None:
        """前提: ケーブル類 / 操作: テンプレートに保存せずに項目「色」を追加して登録する / 期待: テンプレート外の項目として残る。"""
        response: Response = _post(client, '/items/new', {**_cable_form(box), 'new_field_label': '色', 'action': 'add_field'})
        assert 'name="extra__x:色"' in response.text
        response = _post(client, '/items/new', {**_cable_form(box), 'extra__x:色': '黒'})
        assert 'テンプレート外の項目' in response.text
        assert '<dt>色</dt><dd>黒</dd>' in response.text

    def test_photo_can_be_attached_on_registration(self, client: TestClient, box: str) -> None:
        """前提: 箱A-01 / 操作: 写真付きで登録する / 期待: 詳細画面に縮小版が表示され、画像が配信される。"""
        response: Response = _post(
            client, '/items/new', {**_cable_form(box), 'photo_purpose': '正面'}, files=[('photos', ('front.jpg', image_bytes(), 'image/jpeg'))]
        )
        thumb: re.Match[str] | None = re.search(r'src="(/media/thumbs/[^"]+)"', response.text)
        assert thumb is not None
        assert client.get(thumb.group(1)).status_code == 200

    def test_html_is_escaped(self, client: TestClient, box: str) -> None:
        """前提: なし / 操作: 名称にHTMLタグを含めて登録する / 期待: タグとして解釈されずに表示される。"""
        response: Response = _post(client, '/items/new', _cable_form(box, name='<script>alert(1)</script>'))
        assert '<script>alert(1)</script>' not in response.text
        assert '&lt;script&gt;' in response.text


class TestUpdateFlow:
    def test_edit_saves_changes(self, client: TestClient, box: str) -> None:
        """前提: 登録済みのケーブル / 操作: 内容の修正で備考を書き換える / 期待: 詳細画面に反映され、履歴に修正が残る。"""
        item_id: str = _item_id(_post(client, '/items/new', _cable_form(box)))
        assert client.get(f'/items/{item_id}/edit').status_code == 200
        response: Response = _post(
            client,
            f'/items/{item_id}/edit',
            {**_cable_form(box), 'name': 'HDMI - DisplayPort 1.8m', 'note': 'モニター用', 'prev_category_id': str(CABLES_ID)},
        )
        assert '修正しました' in response.text
        assert 'モニター用' in response.text
        assert '<span class="badge">修正</span>' in response.text

    def test_changing_category_in_edit_keeps_values_as_extras(self, client: TestClient, box: str) -> None:
        """前提: 登録済みのケーブル / 操作: 内容の修正でカテゴリを書籍に変える / 期待: 端子と長さがテンプレート外の項目として表示される。"""
        item_id: str = _item_id(_post(client, '/items/new', _cable_form(box)))
        response: Response = _post(
            client, f'/items/{item_id}/edit', {**_cable_form(box), 'major_id': str(BOOKS_ID), 'prev_category_id': str(CABLES_ID), 'action': 'refresh'}
        )
        assert 'テンプレート外の項目' in response.text
        assert 'name="extra__length" type="text" value="1.8m"' in response.text

    def test_saving_category_change_without_refresh_preserves_attributes(self, client: TestClient, conn: sqlite3.Connection, box: str) -> None:
        """前提: ケーブルの編集フォーム / 操作: 表示を更新せず書籍カテゴリで保存する / 期待: 元の属性がテンプレート外に残る。"""
        item_id: str = _item_id(_post(client, '/items/new', _cable_form(box)))
        response: Response = _post(
            client,
            f'/items/{item_id}/edit',
            {**_cable_form(box), 'major_id': str(BOOKS_ID), 'name': '転用した物', 'prev_category_id': str(CABLES_ID)},
        )
        assert response.status_code == 200
        saved: items.ItemDetail = items.get_item(conn, int(item_id))
        assert saved.category_id == BOOKS_ID
        assert saved.attributes == {
            'connector_a': 'HDMI',
            'connector_b': 'DisplayPort',
            'length': {'value': 1.8, 'unit': 'm'},
        }
        assert 'テンプレート外の項目' in response.text

    def test_search_pages_show_total_and_preserve_filters(self, client: TestClient, conn: sqlite3.Connection, places: Places) -> None:
        """前提: 同じ条件の書籍が51件 / 操作: 絞り込み検索から次のページへ進む / 期待: 総件数と全件の到達可能性を保つ。"""
        for number in range(51):
            items.register_item(conn, book_input(places.box_a1, title=f'検証書{number:03d}', isbn=''))
        response: Response = client.get('/', params={'searched': '1', 'status': 'stored', 'category_id': str(BOOKS_ID), 'q': '検証書'})
        assert response.status_code == 200
        assert '全51件（1〜50件目を表示）' in response.text
        assert len(re.findall(r'<td><a href="/items/\d+">', response.text)) == 50
        match: re.Match[str] | None = re.search(r'<a href="([^"]+)">次のページ</a>', response.text)
        assert match is not None
        next_url: str = unescape(match.group(1))
        assert parse_qs(urlparse(next_url).query) == {
            'searched': ['1'],
            'status': ['stored'],
            'category_id': [str(BOOKS_ID)],
            'q': ['検証書'],
            'page': ['2'],
        }
        next_page: Response = client.get(next_url)
        assert '全51件（51〜51件目を表示）' in next_page.text
        assert len(re.findall(r'<td><a href="/items/\d+">', next_page.text)) == 1
        assert '検証書000' in next_page.text
        assert '前のページ' in next_page.text
        assert '次のページ' not in next_page.text

    def test_move_item(self, client: TestClient, box: str) -> None:
        """前提: 箱A-01のケーブルと押入れ / 操作: 押入れへ移動する / 期待: 保管場所が変わる。"""
        item_id: str = _item_id(_post(client, '/items/new', _cable_form(box)))
        closet: Response = _post(client, '/containers', {'name': '押入れ', 'kind_id': '2', 'parent_id': '', 'label': ''})
        closet_id: str = str(closet.url).split('/containers/')[1].split('?')[0]
        response: Response = _post(client, f'/items/{item_id}/move', {'container_id': closet_id})
        assert '移動しました' in response.text
        assert '<a href="/containers/' + closet_id + '">押入れ</a>' in response.text

    def test_search_finds_registered_item(self, client: TestClient, box: str) -> None:
        """前提: 登録済みのケーブル / 操作: 全角でキーワード検索する / 期待: 一覧に表示される。"""
        _post(client, '/items/new', _cable_form(box))
        response: Response = client.get('/', params={'q': 'ｄｉｓｐｌａｙ', 'searched': '1', 'status': ['stored', 'in_use', 'lent']})
        assert 'HDMI - DisplayPort 1.8m' in response.text
        assert '1件' in response.text

    def test_photo_purpose_and_detach(self, client: TestClient, box: str) -> None:
        """前提: 写真付きのケーブル / 操作: 用途を変え、紐付けを解除する / 期待: それぞれ反映される。"""
        item_id: str = _item_id(_post(client, '/items/new', _cable_form(box)))
        _post(client, f'/items/{item_id}/photos', {'photo_purpose': '正面'}, files=[('photos', ('a.jpg', image_bytes(), 'image/jpeg'))])
        detail: str = client.get(f'/items/{item_id}').text
        photo_id: str = re.findall(r'/photos/(\d+)/purpose', detail)[0]
        assert '用途を変更しました' in _post(client, f'/photos/{photo_id}/purpose', {'purpose': '裏面'}).text
        response: Response = _post(client, f'/photos/{photo_id}/detach', {})
        assert '写真の紐付けを解除しました' in response.text
        assert '写真はありません' in response.text

    def test_duplicate_photo_is_warned(self, client: TestClient, box: str) -> None:
        """前提: 写真付きのケーブル / 操作: 同じ写真をもう一度追加する / 期待: 二重取り込みの警告が表示される。"""
        item_id: str = _item_id(_post(client, '/items/new', _cable_form(box)))
        files: list[tuple[str, tuple[str, bytes, str]]] = [('photos', ('a.jpg', image_bytes(), 'image/jpeg'))]
        _post(client, f'/items/{item_id}/photos', {}, files=files)
        response: Response = _post(client, f'/items/{item_id}/photos', {}, files=files)
        assert '同じ内容の写真が既に取り込まれています' in response.text


class TestManageFlow:
    def test_container_contents_edit_and_cycle_error(self, client: TestClient, box: str) -> None:
        """前提: 書斎 › 箱A-01にケーブル / 操作: 書斎の中身を配下込みで表示し、書斎を箱の中へ移す / 期待: 中身が出て、移動は拒否される。"""
        _post(client, '/items/new', _cable_form(box))
        assert 'HDMI - DisplayPort 1.8m' not in client.get('/containers/1').text
        assert 'HDMI - DisplayPort 1.8m' in client.get('/containers/1', params={'nested': '1'}).text
        response: Response = _post(client, '/containers/1/move', {'parent_id': box})
        assert response.status_code == 422
        assert '自分自身や配下の保管場所の中へは移動できません' in response.text
        response = _post(client, f'/containers/{box}/edit', {'name': 'ケーブル箱', 'kind_id': '4', 'label': 'a-09'})
        assert '書斎 › ケーブル箱 [A-09]' in response.text

    def test_container_label_error_and_kind(self, client: TestClient) -> None:
        """前提: なし / 操作: 不正なラベルIDで作成し、種類を追加する / 期待: エラーが表示され、種類は追加される。"""
        response: Response = _post(client, '/containers', {'name': '箱', 'kind_id': '4', 'parent_id': '', 'label': 'A1'})
        assert response.status_code == 422
        assert 'ラベルIDは' in response.text
        assert '引き出し' in _post(client, '/container-kinds', {'kind_name': '引き出し'}).text

    def test_category_and_field_management(self, client: TestClient) -> None:
        """前提: なし / 操作: カテゴリを作り、名前を変え、項目を追加・更新する / 期待: それぞれ反映される。"""
        response: Response = _post(client, '/categories', {'name': '家電', 'parent_id': ''})
        category_id: str = str(response.url).split('/categories/')[1].split('?')[0]
        response = _post(client, f'/categories/{category_id}/rename', {'name': '家電製品'})
        assert '<h1>家電製品</h1>' in response.text
        response = _post(
            client, f'/categories/{category_id}/fields', {'label': '型番', 'field_type': 'text', 'identifier_kind': 'model', 'key': 'model_no'}
        )
        assert '<code>model_no</code>' in response.text
        field_id: str = re.findall(r'action="/fields/(\d+)"', response.text)[0]
        response = _post(client, f'/fields/{field_id}', {'label': 'メーカー型番', 'sort_order': '5', 'required': 'on'})
        assert 'value="メーカー型番"' in response.text
        response = _post(client, f'/categories/{category_id}/fields', {'label': '長さ', 'field_type': 'measure', 'unit_kind': ''})
        assert response.status_code == 422
        assert '単位の種類を選んでください' in response.text

    def test_tag_management(self, client: TestClient) -> None:
        """前提: なし / 操作: タグを作り、名前を変え、同名を作る / 期待: 作成・変更でき、同名は拒否される。"""
        _post(client, '/tags', {'name': '売却候補'})
        tag_id: str = re.findall(r'action="/tags/(\d+)/rename"', client.get('/tags').text)[0]
        assert 'value="手放し候補"' in _post(client, f'/tags/{tag_id}/rename', {'name': '手放し候補'}).text
        response: Response = _post(client, '/tags', {'name': '手放し候補'})
        assert response.status_code == 422
        assert '同じ名前のタグが既にあります' in response.text


class TestStatusFlow:
    def test_lend_sell_and_undo(self, client: TestClient, box: str) -> None:
        """前提: 箱A-01のケーブル / 操作: 貸し出し、売却し、手放しを取り消す / 期待: 各段階の状態と貸出の情報が表示され、取消で貸出中に戻る。"""
        item_id: str = _item_id(_post(client, '/items/new', _cable_form(box)))
        response: Response = _post(client, f'/items/{item_id}/status/lend', {'date': '2026-10-01', 'party': '佐藤さん', 'note': '年末に返却'})
        assert '貸出を記録しました' in response.text
        assert '<dt>貸出の相手</dt><dd>佐藤さん</dd>' in response.text
        response = _post(client, f'/items/{item_id}/status/sell', {'date': '2026-10-05', 'party': '中古店', 'note': ''})
        assert '<span class="status status-sold">売却済</span>' in response.text
        assert '手放しの取消' in response.text
        response = _post(client, f'/items/{item_id}/status/undo_release', {})
        assert '手放しを取り消しました' in response.text
        assert '<dt>貸出の相手</dt><dd>佐藤さん</dd>' in response.text
        assert '<dt>戻し先</dt>' in response.text

    def test_take_out_and_put_back(self, client: TestClient, box: str) -> None:
        """前提: 箱A-01のケーブル / 操作: 取り出してから戻す / 期待: 使用中を経て保管中に戻る。"""
        item_id: str = _item_id(_post(client, '/items/new', _cable_form(box)))
        assert '<span class="status status-in_use">使用中</span>' in _post(client, f'/items/{item_id}/status/take_out', {}).text
        assert '<span class="status status-stored">保管中</span>' in _post(client, f'/items/{item_id}/status/put_back', {}).text

    def test_lend_error_keeps_input_open(self, client: TestClient, box: str) -> None:
        """前提: 箱A-01のケーブル / 操作: 貸出先を空にして貸し出す / 期待: 貸出の入力欄が開いたままエラーが出て、備考は残る。"""
        item_id: str = _item_id(_post(client, '/items/new', _cable_form(box)))
        response: Response = _post(client, f'/items/{item_id}/status/lend', {'date': '2026-10-01', 'party': '', 'note': '年末に返却'})
        assert response.status_code == 422
        assert '相手を入力してください' in response.text
        assert re.search(r'<details class="operation" open>\s*<summary>貸出</summary>', response.text)
        assert '年末に返却</textarea>' in response.text

    def test_wrong_operation_shows_message(self, client: TestClient, box: str) -> None:
        """前提: 保管中のケーブル / 操作: 返却を送る・存在しない操作を送る / 期待: 状態に合わない旨の表示と、見つからない画面になる。"""
        item_id: str = _item_id(_post(client, '/items/new', _cable_form(box)))
        response: Response = _post(client, f'/items/{item_id}/status/give_back', {})
        assert response.status_code == 422
        assert '保管中の物は返却できません' in response.text
        assert _post(client, f'/items/{item_id}/status/explode', {}).status_code == 404

    def test_delete_and_restore(self, client: TestClient, box: str) -> None:
        """前提: ケーブル / 操作: 理由なし・理由付きで削除し、削除済みを探して復元 / 期待: 理由なしは拒否、削除後は検索に出ず、復元で戻る。"""
        item_id: str = _item_id(_post(client, '/items/new', _cable_form(box)))
        response: Response = _post(client, f'/items/{item_id}/delete', {'reason': ''})
        assert response.status_code == 422
        assert '削除理由を入力してください' in response.text
        response = _post(client, f'/items/{item_id}/delete', {'reason': '二重登録'})
        assert '削除済みの登録' in response.text
        assert f'action="/items/{item_id}/status/' not in response.text
        assert 'HDMI - DisplayPort' not in client.get('/', params={'q': 'HDMI'}).text
        found: str = client.get('/', params={'q': 'HDMI', 'searched': '1', 'status': 'stored', 'deleted': '1'}).text
        assert '<span class="badge">削除済み</span>' in found
        response = _post(client, f'/items/{item_id}/restore', {})
        assert '復元しました' in response.text
        assert 'HDMI - DisplayPort' in client.get('/', params={'q': 'HDMI'}).text

    def test_photos_of_deleted_item_are_read_only(self, client: TestClient, box: str) -> None:
        """前提: 写真付きのケーブル / 操作: 削除してから詳細を開き、用途変更を送る / 期待: 写真の変更フォームは出ず、送信は拒否される。"""
        item_id: str = _item_id(_post(client, '/items/new', _cable_form(box)))
        _post(client, f'/items/{item_id}/photos', {'photo_purpose': '正面'}, files=[('photos', ('a.jpg', image_bytes(), 'image/jpeg'))])
        photo_id: str = re.findall(r'/photos/(\d+)/purpose', client.get(f'/items/{item_id}').text)[0]
        detail: str = _post(client, f'/items/{item_id}/delete', {'reason': '誤登録'}).text
        assert '/photos/' + photo_id + '/purpose' not in detail
        assert '/photos/' + photo_id + '/detach' not in detail
        response: Response = _post(client, f'/photos/{photo_id}/purpose', {'purpose': '裏面'})
        assert response.status_code == 422
        assert '削除済みの登録の写真は変更できません' in response.text

    def test_revert_from_history(self, client: TestClient, box: str) -> None:
        """前提: 備考を上書きしたケーブル / 操作: 履歴から上書き前の差分を確認して差し戻す / 期待: 差分が表示され、備考が元に戻る。"""
        item_id: str = _item_id(_post(client, '/items/new', _cable_form(box, note='モニター用')))
        _post(
            client,
            f'/items/{item_id}/edit',
            {**_cable_form(box), 'name': 'HDMI - DisplayPort 1.8m', 'note': '', 'prev_category_id': str(CABLES_ID)},
        )
        detail: str = client.get(f'/items/{item_id}').text
        revert_url: str = re.findall(r'href="(/items/\d+/revert/\d+)"', detail)[0]
        page: Response = client.get(revert_url)
        assert '<td>備考</td><td class="pre"></td><td class="pre">モニター用</td>' in page.text
        assert f'name="{CSRF_FIELD}" value="{client.cookies[CSRF_COOKIE]}"' in page.text
        response: Response = _post(client, revert_url, {})
        assert '履歴から差し戻しました' in response.text
        assert '<dt>備考</dt><dd class="pre">モニター用</dd>' in response.text


class TestBackupFlow:
    def test_backup_is_written(self, client: TestClient, box: str, tmp_path: Path) -> None:
        """前提: ケーブル1件 / 操作: 出力先を指定してバックアップする / 期待: 作成結果が表示され、zipができる。"""
        _post(client, '/items/new', _cable_form(box))
        destination: Path = tmp_path / 'backups'
        destination.mkdir()
        response: Response = _post(client, '/backup', {'destination': str(destination)})
        assert response.status_code == 200
        assert 'バックアップを作成しました' in response.text
        assert len(list(destination.glob('okiba-backup-*.zip'))) == 1

    def test_incomplete_backup_is_distinguished(self, client: TestClient, settings: Settings, box: str, tmp_path: Path) -> None:
        """前提: 元画像が壊れ、閲覧用画像もない写真 / 操作: バックアップする / 期待: 成功ではなく不完全なバックアップと表示される。"""
        _post(client, '/items/new', _cable_form(box), files=[('photos', ('a.jpg', image_bytes(), 'image/jpeg'))])
        original: Path = next((settings.photos_dir / 'originals').rglob('*.jpg'))
        original.write_bytes(original.read_bytes()[:200])
        next((settings.photos_dir / 'display').rglob('*.jpg')).unlink()
        destination: Path = tmp_path / 'backups'
        destination.mkdir()
        response: Response = _post(client, '/backup', {'destination': str(destination)})
        assert '不完全なバックアップです' in response.text
        assert 'バックアップを作成しました' not in response.text
        assert len(list(destination.glob('okiba-backup-*-incomplete.zip'))) == 1

    def test_backup_destination_error(self, client: TestClient) -> None:
        """前提: なし / 操作: 相対パスを出力先にする / 期待: 入力エラーが表示される。"""
        response: Response = _post(client, '/backup', {'destination': 'backups'})
        assert response.status_code == 422
        assert '絶対パスで入力してください' in response.text

    def test_updates_pause_while_backing_up(self, client: TestClient) -> None:
        """前提: バックアップ中 / 操作: タグを作成し、検索画面を開く / 期待: 更新は503で断られ、閲覧はできる。"""
        gate: WriteGate = client.app.state.write_gate  # type: ignore[attr-defined]
        with gate.backup():
            response: Response = _post(client, '/tags', {'name': '新しいタグ'})
            assert response.status_code == 503
            assert 'バックアップ中です' in response.text
            assert client.get('/').status_code == 200
        assert _post(client, '/tags', {'name': '新しいタグ'}).status_code == 200


class TestCsrf:
    def _tag_count(self, client: TestClient) -> int:
        return len(re.findall(r'action="/tags/(\d+)/rename"', client.get('/tags').text))

    def test_cookie_is_http_only_and_same_site_strict(self, settings: Settings) -> None:
        """前提: Cookieなし / 操作: 画面を開く / 期待: スクリプトから読めず、別サイトからの送信に付かないCookieでトークンが配られる。"""
        with TestClient(create_app(settings), base_url='http://127.0.0.1') as fresh:
            header: str = fresh.get('/').headers['set-cookie']
        assert header.startswith(f'{CSRF_COOKIE}=')
        assert 'HttpOnly' in header
        assert 'SameSite=strict' in header

    @pytest.mark.parametrize(
        ('token', 'origin'),
        [(None, None), ('x' * 43, None), ('valid', 'https://evil.example')],
        ids=['トークンなし', 'トークン不一致', '別サイトからの送信'],
    )
    def test_forged_post_is_rejected(self, client: TestClient, token: str | None, origin: str | None) -> None:
        """前提: なし / 操作: 正しいトークンを持たない、または別サイトからのフォーム送信 / 期待: 403で拒否され、データは変わらない。"""
        data: dict[str, str] = {'name': '不正なタグ'}
        if token is not None:
            data[CSRF_FIELD] = client.cookies[CSRF_COOKIE] if token == 'valid' else token
        headers: dict[str, str] = {'origin': origin} if origin else {}
        response: Response = client.post('/tags', data=data, headers=headers)
        assert response.status_code == 403
        assert '送信できません' in response.text
        assert self._tag_count(client) == 0

    def test_same_origin_post_with_token_is_accepted(self, client: TestClient) -> None:
        """前提: なし / 操作: 同じオリジンから正しいトークンで送信する / 期待: 受け付けられる。"""
        response: Response = client.post(
            '/tags', data={'name': 'タグ', CSRF_FIELD: client.cookies[CSRF_COOKIE]}, headers={'origin': 'http://127.0.0.1'}
        )
        assert response.status_code == 200
        assert self._tag_count(client) == 1

    def test_untrusted_host_is_rejected_before_cookie_or_routes(self, settings: Settings) -> None:
        """前提: 外部ドメイン名をHostにしたアクセス / 操作: 閲覧・任意Cookie付き更新・静的ファイル取得 / 期待: すべて拒否する。"""
        with TestClient(create_app(settings), base_url='http://attacker.example') as foreign:
            token: str = 'A' * 43
            foreign.cookies.set(CSRF_COOKIE, token)
            rejected: Response = foreign.get('/')
            assert rejected.status_code == 400
            assert 'set-cookie' not in rejected.headers
            assert foreign.get('/static/style.css').status_code == 400
            assert (
                foreign.post('/tags', data={'name': '不正なタグ', CSRF_FIELD: token}, headers={'origin': 'http://attacker.example'}).status_code
                == 400
            )

    @pytest.mark.parametrize(
        'url', ['/items/new', '/items/{id}', '/categories', f'/categories/{CABLES_ID}', '/tags', '/containers', '/containers/{id}', '/backup']
    )
    def test_every_post_form_carries_token(self, client: TestClient, box: str, url: str) -> None:
        """前提: 保管場所と写真付き物品 / 操作: フォームのある画面を開く / 期待: POSTフォームすべてにトークンがある。"""
        _post(client, '/tags', {'name': '既存タグ'})
        if url == '/items/{id}':
            item_id: str = _item_id(_post(client, '/items/new', _cable_form(box)))
            upload: Response = _post(client, f'/items/{item_id}/photos', {}, files=[('photos', ('test.jpg', image_bytes(), 'image/jpeg'))])
            assert upload.status_code == 200
            url = url.format(id=item_id)
        elif url == '/containers/{id}':
            url = url.format(id=box)
        response: Response = client.get(url)
        assert response.status_code == 200
        html: str = response.text
        if '/items/' in url and url != '/items/new':
            assert re.search(r'action="/photos/\d+/purpose"', html)
        forms: list[str] = re.findall(r'<form[^>]*method="post"[^>]*>.*?</form>', html, flags=re.DOTALL)
        assert forms
        token: str = client.cookies[CSRF_COOKIE]
        assert all(f'name="{CSRF_FIELD}" value="{token}"' in form for form in forms)

"""画面のHTTPシナリオのテスト。

依存パッケージ（FastAPI・Starlette・Jinja2・python-multipart）の更新で画面の動作が壊れていないことを確認する。
"""

import re

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from tests.helpers import BOOKS_ID, CABLES_ID, image_bytes


def _post(client: TestClient, url: str, data: dict[str, str], files: list[tuple[str, tuple[str, bytes, str]]] | None = None) -> Response:
    response: Response = client.post(url, data=data, files=files)
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
    @pytest.mark.parametrize('url', ['/', '/items/new', '/manage', '/categories', f'/categories/{BOOKS_ID}', '/tags', '/containers'])
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
        response: Response = client.post(
            '/items/new', data={**_cable_form(box), 'photo_purpose': '正面'}, files=[('photos', ('front.jpg', image_bytes(), 'image/jpeg'))]
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
        client.post(f'/items/{item_id}/photos', data={'photo_purpose': '正面'}, files=[('photos', ('a.jpg', image_bytes(), 'image/jpeg'))])
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
        client.post(f'/items/{item_id}/photos', data={}, files=files)
        response: Response = client.post(f'/items/{item_id}/photos', data={}, files=files)
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

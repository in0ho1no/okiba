"""画面処理で共有する依存関係・テンプレート・補助関数。"""

import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from okiba import catalog, db
from okiba.catalog import FIELD_TYPES, IDENTIFIER_KINDS, UNIT_KINDS, UNITS
from okiba.config import Settings
from okiba.events import Event
from okiba.items import STATUS_LABELS, display_value
from okiba.photos import PURPOSE_SUGGESTIONS

TEMPLATES_DIR: Path = Path(__file__).parent / 'templates'
STATIC_DIR: Path = Path(__file__).parent / 'static'

templates: Jinja2Templates = Jinja2Templates(directory=TEMPLATES_DIR)

NOTICES: dict[str, str] = {
    'registered': '登録しました。',
    'updated': '修正しました。',
    'moved': '移動しました。',
    'created': '作成しました。',
    'photo_added': '写真を追加しました。',
    'photo_detached': '写真の紐付けを解除しました。',
    'photo_purpose': '写真の用途を変更しました。',
    'unchanged': '変更はありませんでした。',
    'taken_out': '取り出しました。',
    'put_back': '戻しました。',
    'lent': '貸出を記録しました。',
    'given_back': '返却を記録しました。',
    'sold': '売却を記録しました。',
    'disposed': '廃棄を記録しました。',
    'release_undone': '手放しを取り消しました。',
    'deleted': '削除しました。「削除済みも表示」で検索すると確認・復元できます。',
    'restored': '復元しました。',
    'reverted': '履歴から差し戻しました。',
}

_SNAPSHOT_LABELS: dict[str, str] = {
    'name': '名称',
    'category_id': 'カテゴリ',
    'quantity': '数量',
    'status': '状態',
    'container_id': '保管場所',
    'note': '備考',
    'status_date': '日付',
    'status_party': '相手',
    'status_note': '貸出・手放しの備考',
    'deleted': '削除状態',
    'attributes': '属性',
    'tags': 'タグ',
    'identifiers': '識別コード',
    'photos': '写真',
    'parent_id': '親の保管場所',
    'label': 'ラベルID',
    'kind_id': '種類',
    'retired': '廃止状態',
}


def event_changes(event: Event) -> list[str]:
    """変更履歴で値が変わった項目の表示名を返す。"""
    if event.before is None or event.after is None:
        return []
    return [label for key, label in _SNAPSHOT_LABELS.items() if event.before.get(key) != event.after.get(key)]


templates.env.globals.update(
    STATUS_LABELS=STATUS_LABELS,
    FIELD_TYPES=FIELD_TYPES,
    IDENTIFIER_KINDS=IDENTIFIER_KINDS,
    UNIT_KINDS=UNIT_KINDS,
    UNITS=UNITS,
    PURPOSE_SUGGESTIONS=PURPOSE_SUGGESTIONS,
    display_value=display_value,
    event_changes=event_changes,
)


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """リクエストごとにDB接続を開き、終了時に閉じる。"""
    settings: Settings = request.app.state.settings
    conn: sqlite3.Connection = db.connect(settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


def render(request: Request, name: str, context: Mapping[str, Any], status_code: int = 200) -> HTMLResponse:
    """テンプレートを描画する。クエリの notice を画面上部のお知らせとして表示する。"""
    notice_code: str = request.query_params.get('notice', '')
    notice: str = NOTICES.get(notice_code, '')
    duplicate: str = request.query_params.get('dup', '')
    if duplicate:
        notice = f'{notice} 同じ内容の写真が既に取り込まれています（{duplicate}）。'.strip()
    # フォームに埋め込むCSRF対策のトークン。テンプレートでは {{ security.csrf_token }} で参照する。
    security: dict[str, str] = {'csrf_token': getattr(request.state, 'csrf_token', '')}
    full_context: dict[str, Any] = {'notice': notice, 'errors': {}, 'security': security, **context}
    return templates.TemplateResponse(request, name, full_context, status_code=status_code)


def redirect(url: str) -> RedirectResponse:
    """POST後の再送信を防ぐため、303で画面を移動する。"""
    return RedirectResponse(url, status_code=303)


def optional_int(raw: object) -> int | None:
    """フォーム・クエリの値を整数に変換する。空や不正な値は None。"""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def text_value(raw: object) -> str:
    """フォームの値を文字列にする。ファイルなど文字列以外は空文字。"""
    return raw if isinstance(raw, str) else ''


def category_selection(conn: sqlite3.Connection, category_id: int) -> tuple[int, int | None]:
    """カテゴリIDを（大カテゴリID, 小カテゴリID）に分ける。"""
    chain: list[catalog.Category] = catalog.category_chain(conn, category_id)
    if len(chain) == 1:
        return chain[0].id, None
    return chain[0].id, chain[1].id

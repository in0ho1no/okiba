"""状態の変更（取り出す・戻す・貸出・返却・手放し・手放しの取消）と、誤登録削除・復元。

数量の一部だけを対象にする自動分割はフェーズ1-3で扱い、ここでは登録の全数量を対象にする。
"""

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any

from okiba import events
from okiba.common import NotFoundError, ValidationError, clean_text, now_iso
from okiba.containers import Container, container_path, get_container
from okiba.db import transaction
from okiba.items import ACTIVE_STATUSES, STATUS_LABELS, refresh_search_index
from okiba.snapshots import item_snapshot

RELEASED_STATUSES: tuple[str, ...] = ('sold', 'disposed')

RELEASE_LABELS: dict[str, str] = {'sold': '売却', 'disposed': '廃棄'}

# 状態に関わる列はすべて同じ固定のSQLで書き換え、操作ごとにSQLを組み立てない。
_SET_STATUS_SQL: str = (
    'UPDATE items SET status = ?, container_id = ?, status_date = ?, status_party = ?, status_note = ?, release_event_id = ?, updated_at = ? '
    'WHERE id = ?'
)


@dataclass(frozen=True)
class StatusRecord:
    """貸出・売却・廃棄で記録する日付・相手・備考。日付は YYYY-MM-DD。"""

    date: str
    party: str = ''
    note: str = ''


def _editable(conn: sqlite3.Connection, item_id: int) -> dict[str, Any]:
    before: dict[str, Any] = item_snapshot(conn, item_id)
    if before['deleted'] or before['merged_into_id'] is not None:
        raise ValidationError({'item': '削除済み・統合済みの登録は状態を変更できません。'})
    return before


def _require_status(before: dict[str, Any], allowed: tuple[str, ...], refusal: str) -> None:
    if before['status'] not in allowed:
        raise ValidationError({'item': f'{STATUS_LABELS[before["status"]]}の物は{refusal}。'})


def _check_record(record: StatusRecord, *, party_required: bool) -> StatusRecord:
    errors: dict[str, str] = {}
    raw_date: str = record.date.strip()
    try:
        parsed: date | None = date.fromisoformat(raw_date) if len(raw_date) == len('YYYY-MM-DD') else None
    except ValueError:
        parsed = None
    if parsed is None:
        errors['date'] = '日付を YYYY-MM-DD の形式で入力してください。'
    party: str = clean_text(record.party)
    if party_required and not party:
        errors['party'] = '相手を入力してください。'
    if errors:
        raise ValidationError(errors)
    assert parsed is not None
    return StatusRecord(date=parsed.isoformat(), party=party, note=record.note.strip())


def _destination(conn: sqlite3.Connection, container_id: int, replacement: int | None) -> tuple[int, str]:
    """戻る先の保管場所と、履歴に残すメモを返す。戻る先が廃止済みなら、選び直した有効な保管場所を使う。"""
    original: Container = get_container(conn, container_id)
    if not original.retired:
        return container_id, ''
    original_path: str = container_path(conn, container_id)
    if replacement is None:
        raise ValidationError({'container_id': f'戻る先の保管場所「{original_path}」は廃止済みです。有効な保管場所を選んでください。'})
    try:
        chosen: Container = get_container(conn, replacement)
    except NotFoundError as error:
        raise ValidationError({'container_id': '保管場所を選んでください。'}) from error
    if chosen.retired:
        raise ValidationError({'container_id': '廃止済みの保管場所は選べません。'})
    return replacement, f'戻る先「{original_path}」が廃止済みのため「{container_path(conn, replacement)}」を選び直し'


def _memo(*parts: str) -> str:
    return '：'.join(part for part in parts if part)


def _set_status(
    conn: sqlite3.Connection,
    item_id: int,
    status: str,
    container_id: int | None,
    record: StatusRecord | None,
    release_event_id: int | None = None,
) -> None:
    conn.execute(
        _SET_STATUS_SQL,
        (
            status,
            container_id,
            record.date if record else None,
            (record.party or None) if record else None,
            (record.note or None) if record else None,
            release_event_id,
            now_iso(),
            item_id,
        ),
    )
    refresh_search_index(conn, item_id)


def _change(
    conn: sqlite3.Connection,
    item_id: int,
    before: dict[str, Any],
    status: str,
    container_id: int | None,
    record: StatusRecord | None,
    memo: str,
    release_event_id: int | None = None,
) -> int:
    with transaction(conn):
        _set_status(conn, item_id, status, container_id, record, release_event_id)
        return events.record(conn, 'item', item_id, 'status', before, item_snapshot(conn, item_id), memo)


def take_out(conn: sqlite3.Connection, item_id: int) -> None:
    """保管中の物を取り出し、使用中にする。保管場所は戻し先として保持する。"""
    before: dict[str, Any] = _editable(conn, item_id)
    _require_status(before, ('stored',), '取り出せません')
    _change(conn, item_id, before, 'in_use', before['container_id'], None, '取り出す')


def put_back(conn: sqlite3.Connection, item_id: int, container_id: int | None = None) -> None:
    """使用中の物を戻し先へ戻す。戻し先が廃止済みの場合は container_id に選び直した保管場所を渡す。"""
    before: dict[str, Any] = _editable(conn, item_id)
    _require_status(before, ('in_use',), '戻せません')
    destination: int
    note: str
    destination, note = _destination(conn, before['container_id'], container_id)
    _change(conn, item_id, before, 'stored', destination, None, _memo('戻す', note))


def lend(conn: sqlite3.Connection, item_id: int, record: StatusRecord) -> None:
    """保管中・使用中の物を貸し出す。元の保管場所は戻し先として保持する。"""
    before: dict[str, Any] = _editable(conn, item_id)
    _require_status(before, ('stored', 'in_use'), '貸し出せません')
    checked: StatusRecord = _check_record(record, party_required=True)
    _change(conn, item_id, before, 'lent', before['container_id'], checked, '貸出')


def give_back(conn: sqlite3.Connection, item_id: int, container_id: int | None = None) -> None:
    """貸出中の物の返却を記録し、戻し先へ保管中として戻す。貸出の情報は変更履歴にだけ残す。"""
    before: dict[str, Any] = _editable(conn, item_id)
    _require_status(before, ('lent',), '返却できません')
    destination: int
    note: str
    destination, note = _destination(conn, before['container_id'], container_id)
    _change(conn, item_id, before, 'stored', destination, None, _memo('返却', note))


def release(conn: sqlite3.Connection, item_id: int, status: str, record: StatusRecord) -> None:
    """所持中の物を売却済・廃棄済にする。保管場所は空にし、手放しの取消のため操作の履歴IDを保持する。"""
    if status not in RELEASED_STATUSES:
        raise ValueError(f'手放しの状態ではありません: {status}')
    before: dict[str, Any] = _editable(conn, item_id)
    _require_status(before, ACTIVE_STATUSES, f'{RELEASE_LABELS[status]}できません')
    checked: StatusRecord = _check_record(record, party_required=status == 'sold')
    with transaction(conn):
        event_id: int = _change(conn, item_id, before, status, None, checked, RELEASE_LABELS[status])
        conn.execute('UPDATE items SET release_event_id = ? WHERE id = ?', (event_id, item_id))
        events.replace_after(conn, event_id, item_snapshot(conn, item_id))


def undo_release(conn: sqlite3.Connection, item_id: int, container_id: int | None = None) -> None:
    """現在の手放し操作を取り消し、手放す直前の状態・保管場所と貸出や手放しの情報を戻す。

    名称・属性など、手放した後の編集は巻き戻さない。戻る先が廃止済みの場合は container_id に選び直した保管場所を渡す。
    """
    before: dict[str, Any] = _editable(conn, item_id)
    _require_status(before, RELEASED_STATUSES, '手放しを取り消せません')
    if before['release_event_id'] is None:
        raise ValidationError({'item': '取り消せる手放しの操作が見つかりません。'})
    released: dict[str, Any] | None = events.get_event(conn, before['release_event_id']).before
    if released is None:
        raise ValidationError({'item': '取り消せる手放しの操作が見つかりません。'})
    destination: int
    note: str
    destination, note = _destination(conn, released['container_id'], container_id)
    record: StatusRecord | None = None
    if released['status_date'] is not None:
        record = StatusRecord(date=released['status_date'], party=released['status_party'] or '', note=released['status_note'] or '')
    _change(conn, item_id, before, released['status'], destination, record, _memo('手放しの取消', note), released['release_event_id'])


def delete_item(conn: sqlite3.Connection, item_id: int, reason: str) -> None:
    """誤って登録した物を論理削除する。削除理由は必須で、変更履歴のメモに残す。"""
    before: dict[str, Any] = item_snapshot(conn, item_id)
    if before['deleted']:
        raise ValidationError({'item': 'この登録は既に削除済みです。'})
    if before['merged_into_id'] is not None:
        raise ValidationError({'item': '統合済みの登録は削除できません。'})
    cleaned: str = reason.strip()
    if not cleaned:
        raise ValidationError({'reason': '削除理由を入力してください。'})
    timestamp: str = now_iso()
    with transaction(conn):
        conn.execute('UPDATE items SET deleted = 1, deleted_at = ?, updated_at = ? WHERE id = ?', (timestamp, timestamp, item_id))
        events.record(conn, 'item', item_id, 'delete', before, item_snapshot(conn, item_id), cleaned)


def restore_item(conn: sqlite3.Connection, item_id: int, container_id: int | None = None) -> None:
    """削除済みの登録を復元する。所持中の物の戻る先が廃止済みの場合は container_id に選び直した保管場所を渡す。"""
    before: dict[str, Any] = item_snapshot(conn, item_id)
    if not before['deleted']:
        raise ValidationError({'item': 'この登録は削除されていません。'})
    destination: int | None = before['container_id']
    note: str = ''
    if before['status'] in ACTIVE_STATUSES:
        destination, note = _destination(conn, before['container_id'], container_id)
    with transaction(conn):
        conn.execute(
            'UPDATE items SET deleted = 0, deleted_at = NULL, container_id = ?, updated_at = ? WHERE id = ?', (destination, now_iso(), item_id)
        )
        events.record(conn, 'item', item_id, 'restore', before, item_snapshot(conn, item_id), note)


def retired_destination(conn: sqlite3.Connection, item_id: int) -> str | None:
    """戻す・返却・手放しの取消・復元で戻る先が廃止済みなら、その保管場所の表示名を返す。"""
    snapshot: dict[str, Any] = item_snapshot(conn, item_id)
    container_id: int | None = snapshot['container_id']
    if snapshot['status'] in RELEASED_STATUSES and snapshot['release_event_id'] is not None:
        released: dict[str, Any] | None = events.get_event(conn, snapshot['release_event_id']).before
        container_id = released['container_id'] if released else None
    if container_id is None or not get_container(conn, container_id).retired:
        return None
    return container_path(conn, container_id)

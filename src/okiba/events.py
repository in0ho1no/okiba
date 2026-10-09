"""変更履歴（item_events）の記録と参照。"""

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

from okiba.common import NotFoundError, now_iso

TargetType = Literal['item', 'container']
EventKind = Literal['create', 'update', 'move', 'status', 'split', 'merge', 'delete', 'restore']

EVENT_LABELS: dict[str, str] = {
    'create': '登録',
    'update': '修正',
    'move': '移動',
    'status': '状態変更',
    'split': '分割',
    'merge': '統合',
    'delete': '削除',
    'restore': '復元',
}


@dataclass(frozen=True)
class Event:
    """変更履歴1件。"""

    id: int
    target_type: str
    target_id: int
    kind: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    memo: str
    created_at: str

    @property
    def label(self) -> str:
        """種類の表示名。"""
        return EVENT_LABELS[self.kind]


def _dump(snapshot: dict[str, Any] | None) -> str | None:
    if snapshot is None:
        return None
    return json.dumps(snapshot, ensure_ascii=False, sort_keys=True)


def record(
    conn: sqlite3.Connection,
    target_type: TargetType,
    target_id: int,
    kind: EventKind,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    memo: str = '',
) -> int:
    """変更前後の完全な内容を履歴に記録し、履歴IDを返す。"""
    cursor: sqlite3.Cursor = conn.execute(
        'INSERT INTO item_events (target_type, target_id, kind, before_json, after_json, memo, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (target_type, target_id, kind, _dump(before), _dump(after), memo, now_iso()),
    )
    event_id: int | None = cursor.lastrowid
    assert event_id is not None
    return event_id


def replace_after(conn: sqlite3.Connection, event_id: int, after: dict[str, Any]) -> None:
    """記録直後の履歴の変更後を書き直す。記録した履歴のIDを含めた内容を残すために使う。"""
    conn.execute('UPDATE item_events SET after_json = ? WHERE id = ?', (_dump(after), event_id))


def _event(row: sqlite3.Row) -> Event:
    return Event(
        id=row['id'],
        target_type=row['target_type'],
        target_id=row['target_id'],
        kind=row['kind'],
        before=json.loads(row['before_json']) if row['before_json'] else None,
        after=json.loads(row['after_json']) if row['after_json'] else None,
        memo=row['memo'],
        created_at=row['created_at'],
    )


def get_event(conn: sqlite3.Connection, event_id: int) -> Event:
    """変更履歴を1件返す。"""
    row: sqlite3.Row | None = conn.execute('SELECT * FROM item_events WHERE id = ?', (event_id,)).fetchone()
    if row is None:
        raise NotFoundError(f'event {event_id}')
    return _event(row)


def list_events(conn: sqlite3.Connection, target_type: TargetType, target_id: int) -> list[Event]:
    """対象の変更履歴を新しい順に返す。"""
    rows: list[sqlite3.Row] = conn.execute(
        'SELECT * FROM item_events WHERE target_type = ? AND target_id = ? ORDER BY id DESC',
        (target_type, target_id),
    ).fetchall()
    return [_event(row) for row in rows]

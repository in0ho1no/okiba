"""変更履歴に保存する、物品・保管場所のその時点の完全な内容。"""

import json
import sqlite3
from typing import Any

from okiba.common import NotFoundError

# 紐付け先の種類ごとの写真取得SQL。列名を文字列で組み立てず、固定のSQLから選ぶ。
_PHOTOS_SQL: dict[str, str] = {
    'item': 'SELECT id, sha256, purpose, detached FROM photos WHERE item_id = ? ORDER BY id',
    'container': 'SELECT id, sha256, purpose, detached FROM photos WHERE container_id = ? ORDER BY id',
}


def _photos(conn: sqlite3.Connection, target_type: str, target_id: int) -> list[dict[str, Any]]:
    rows: list[sqlite3.Row] = conn.execute(_PHOTOS_SQL[target_type], (target_id,)).fetchall()
    return [{'id': row['id'], 'sha256': row['sha256'], 'purpose': row['purpose'], 'detached': bool(row['detached'])} for row in rows]


def item_snapshot(conn: sqlite3.Connection, item_id: int) -> dict[str, Any]:
    """物品の内容を、属性・タグ・識別コード・写真との対応・貸出や手放しの情報を含めて返す。"""
    row: sqlite3.Row | None = conn.execute('SELECT * FROM items WHERE id = ?', (item_id,)).fetchone()
    if row is None:
        raise NotFoundError(f'item {item_id}')
    tags: list[sqlite3.Row] = conn.execute(
        'SELECT tags.id, tags.name FROM item_tags JOIN tags ON tags.id = item_tags.tag_id WHERE item_tags.item_id = ? ORDER BY tags.id',
        (item_id,),
    ).fetchall()
    identifiers: list[sqlite3.Row] = conn.execute(
        'SELECT kind, value FROM identifiers WHERE item_id = ? ORDER BY kind, value',
        (item_id,),
    ).fetchall()
    return {
        'id': row['id'],
        'name': row['name'],
        'category_id': row['category_id'],
        'quantity': row['quantity'],
        'status': row['status'],
        'container_id': row['container_id'],
        'note': row['note'],
        'attributes': json.loads(row['attributes']),
        'status_date': row['status_date'],
        'status_party': row['status_party'],
        'status_note': row['status_note'],
        'release_event_id': row['release_event_id'],
        'deleted': bool(row['deleted']),
        'split_from_id': row['split_from_id'],
        'merged_into_id': row['merged_into_id'],
        'tags': [{'id': tag['id'], 'name': tag['name']} for tag in tags],
        'identifiers': [{'kind': identifier['kind'], 'value': identifier['value']} for identifier in identifiers],
        'photos': _photos(conn, 'item', item_id),
    }


def container_snapshot(conn: sqlite3.Connection, container_id: int) -> dict[str, Any]:
    """保管場所の内容を、写真との対応を含めて返す。"""
    row: sqlite3.Row | None = conn.execute('SELECT * FROM containers WHERE id = ?', (container_id,)).fetchone()
    if row is None:
        raise NotFoundError(f'container {container_id}')
    return {
        'id': row['id'],
        'name': row['name'],
        'kind_id': row['kind_id'],
        'parent_id': row['parent_id'],
        'label': row['label'],
        'retired': bool(row['retired']),
        'photos': _photos(conn, 'container', container_id),
    }

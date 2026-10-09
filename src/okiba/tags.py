"""タグ。"""

import re
import sqlite3
import unicodedata
from dataclasses import dataclass

from okiba.common import NotFoundError, ValidationError, now_iso
from okiba.db import transaction

_SEPARATORS: re.Pattern[str] = re.compile(r'[,、]')


@dataclass(frozen=True)
class Tag:
    """タグ。"""

    id: int
    name: str
    retired: bool


def _tag(row: sqlite3.Row) -> Tag:
    return Tag(id=row['id'], name=row['name'], retired=bool(row['retired']))


def normalize_tag_name(name: str) -> str:
    """表記の違いで別タグにならないよう、NFKC正規化して前後の空白を除く。"""
    return unicodedata.normalize('NFKC', name).strip()


def parse_tag_text(text: str) -> list[str]:
    """カンマ（, や 、）区切りの入力をタグ名の一覧にする。重複は除き、入力順を保つ。"""
    names: list[str] = []
    for part in _SEPARATORS.split(text):
        name: str = normalize_tag_name(part)
        if name and name not in names:
            names.append(name)
    return names


def list_tags(conn: sqlite3.Connection, include_retired: bool = False) -> list[Tag]:
    """タグを名前順に返す。"""
    rows: list[sqlite3.Row] = conn.execute('SELECT * FROM tags ORDER BY name').fetchall()
    return [_tag(row) for row in rows if include_retired or not row['retired']]


def get_tag(conn: sqlite3.Connection, tag_id: int) -> Tag:
    """タグを1件返す。"""
    row: sqlite3.Row | None = conn.execute('SELECT * FROM tags WHERE id = ?', (tag_id,)).fetchone()
    if row is None:
        raise NotFoundError(f'tag {tag_id}')
    return _tag(row)


def find_tag(conn: sqlite3.Connection, name: str) -> Tag | None:
    """名前でタグを探す。"""
    row: sqlite3.Row | None = conn.execute('SELECT * FROM tags WHERE name = ?', (normalize_tag_name(name),)).fetchone()
    return _tag(row) if row else None


def create_tag(conn: sqlite3.Connection, name: str) -> int:
    """タグを作成する。"""
    normalized: str = normalize_tag_name(name)
    if not normalized:
        raise ValidationError({'name': 'タグ名を入力してください。'})
    if _SEPARATORS.search(normalized):
        raise ValidationError({'name': 'タグ名に「,」「、」は使えません。'})
    if find_tag(conn, normalized) is not None:
        raise ValidationError({'name': '同じ名前のタグが既にあります。'})
    timestamp: str = now_iso()
    with transaction(conn):
        cursor: sqlite3.Cursor = conn.execute(
            'INSERT INTO tags (name, created_at, updated_at) VALUES (?, ?, ?)',
            (normalized, timestamp, timestamp),
        )
    tag_id: int | None = cursor.lastrowid
    assert tag_id is not None
    return tag_id


def rename_tag(conn: sqlite3.Connection, tag_id: int, name: str) -> None:
    """タグ名を変更する。"""
    get_tag(conn, tag_id)
    normalized: str = normalize_tag_name(name)
    if not normalized:
        raise ValidationError({'name': 'タグ名を入力してください。'})
    if _SEPARATORS.search(normalized):
        raise ValidationError({'name': 'タグ名に「,」「、」は使えません。'})
    existing: Tag | None = find_tag(conn, normalized)
    if existing is not None and existing.id != tag_id:
        raise ValidationError({'name': '同じ名前のタグが既にあります。'})
    with transaction(conn):
        conn.execute('UPDATE tags SET name = ?, updated_at = ? WHERE id = ?', (normalized, now_iso(), tag_id))


def ensure_tags(conn: sqlite3.Connection, names: list[str]) -> list[int]:
    """名前に対応するタグIDを返す。未登録のタグは作成する。"""
    tag_ids: list[int] = []
    for name in names:
        existing: Tag | None = find_tag(conn, name)
        tag_ids.append(existing.id if existing else create_tag(conn, name))
    return tag_ids


def item_tag_names(conn: sqlite3.Connection, item_id: int) -> list[str]:
    """物品に付いたタグ名を名前順に返す。"""
    rows: list[sqlite3.Row] = conn.execute(
        'SELECT tags.name FROM item_tags JOIN tags ON tags.id = item_tags.tag_id WHERE item_tags.item_id = ? ORDER BY tags.name',
        (item_id,),
    ).fetchall()
    return [row['name'] for row in rows]

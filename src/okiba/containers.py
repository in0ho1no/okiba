"""保管場所（部屋・家具・段・箱など）とラベルID。"""

import re
import sqlite3
import unicodedata
from dataclasses import dataclass

from okiba import events
from okiba.common import NotFoundError, ValidationError, clean_text, now_iso
from okiba.db import transaction
from okiba.snapshots import container_snapshot

# 箱IDは「英大文字1〜3字-数字2〜4桁」。将来QRコード化しても読み取りやすい短い英数字に限る。
LABEL_PATTERN: re.Pattern[str] = re.compile(r'^([A-Z]{1,3})-(\d{2,4})$')


@dataclass(frozen=True)
class ContainerKind:
    """保管場所の種類。"""

    id: int
    name: str


@dataclass(frozen=True)
class Container:
    """保管場所。"""

    id: int
    name: str
    kind_id: int
    kind_name: str
    parent_id: int | None
    label: str | None
    retired: bool

    @property
    def display_name(self) -> str:
        """ラベルIDがあれば併記した表示名。"""
        return f'{self.name} [{self.label}]' if self.label else self.name


def list_kinds(conn: sqlite3.Connection) -> list[ContainerKind]:
    """保管場所の種類を表示順に返す。"""
    rows: list[sqlite3.Row] = conn.execute('SELECT id, name FROM container_kinds ORDER BY sort_order, id').fetchall()
    return [ContainerKind(id=row['id'], name=row['name']) for row in rows]


def add_kind(conn: sqlite3.Connection, name: str) -> int:
    """保管場所の種類を追加する。"""
    cleaned: str = clean_text(name)
    if not cleaned:
        raise ValidationError({'kind_name': '種類名を入力してください。'})
    if conn.execute('SELECT 1 FROM container_kinds WHERE name = ?', (cleaned,)).fetchone():
        raise ValidationError({'kind_name': '同じ名前の種類が既にあります。'})
    with transaction(conn):
        cursor: sqlite3.Cursor = conn.execute(
            'INSERT INTO container_kinds (name, sort_order) VALUES (?, (SELECT ifnull(max(sort_order), 0) + 10 FROM container_kinds))',
            (cleaned,),
        )
    kind_id: int | None = cursor.lastrowid
    assert kind_id is not None
    return kind_id


def _container(row: sqlite3.Row) -> Container:
    return Container(
        id=row['id'],
        name=row['name'],
        kind_id=row['kind_id'],
        kind_name=row['kind_name'],
        parent_id=row['parent_id'],
        label=row['label'],
        retired=bool(row['retired']),
    )


_SELECT: str = (
    'SELECT containers.*, container_kinds.name AS kind_name FROM containers JOIN container_kinds ON container_kinds.id = containers.kind_id'
)


def list_containers(conn: sqlite3.Connection, include_retired: bool = False) -> list[Container]:
    """保管場所を階層順（親の直後に子）に返す。"""
    rows: list[sqlite3.Row] = conn.execute(f'{_SELECT} ORDER BY containers.name, containers.id').fetchall()
    containers: list[Container] = [_container(row) for row in rows if include_retired or not row['retired']]
    children: dict[int | None, list[Container]] = {}
    known_ids: set[int] = {container.id for container in containers}
    for container in containers:
        parent_key: int | None = container.parent_id if container.parent_id in known_ids else None
        children.setdefault(parent_key, []).append(container)
    ordered: list[Container] = []

    def walk(parent_id: int | None) -> None:
        for child in children.get(parent_id, []):
            ordered.append(child)
            walk(child.id)

    walk(None)
    return ordered


def get_container(conn: sqlite3.Connection, container_id: int) -> Container:
    """保管場所を1件返す。"""
    row: sqlite3.Row | None = conn.execute(f'{_SELECT} WHERE containers.id = ?', (container_id,)).fetchone()
    if row is None:
        raise NotFoundError(f'container {container_id}')
    return _container(row)


def ancestors(conn: sqlite3.Connection, container_id: int) -> list[Container]:
    """最上位から指定した保管場所までの並びを返す。"""
    chain: list[Container] = []
    current: int | None = container_id
    while current is not None:
        container: Container = get_container(conn, current)
        chain.append(container)
        current = container.parent_id
    return list(reversed(chain))


def container_path(conn: sqlite3.Connection, container_id: int) -> str:
    """「部屋 › 家具 › 段 › 箱」形式の表示名を返す。"""
    return ' › '.join(container.display_name for container in ancestors(conn, container_id))


def all_paths(conn: sqlite3.Connection, include_retired: bool = False) -> list[tuple[Container, str]]:
    """選択肢用に、階層順の保管場所と表示名の組を返す。"""
    containers: list[Container] = list_containers(conn, include_retired=True)
    by_id: dict[int, Container] = {container.id: container for container in containers}
    result: list[tuple[Container, str]] = []
    for container in containers:
        if container.retired and not include_retired:
            continue
        names: list[str] = []
        current: Container | None = container
        while current is not None:
            names.append(current.display_name)
            current = by_id.get(current.parent_id) if current.parent_id is not None else None
        result.append((container, ' › '.join(reversed(names))))
    return result


def descendant_ids(conn: sqlite3.Connection, container_id: int) -> set[int]:
    """指定した保管場所自身と、その配下すべての保管場所のIDを返す。"""
    rows: list[sqlite3.Row] = conn.execute(
        'WITH RECURSIVE tree(id) AS (SELECT ? UNION SELECT containers.id FROM containers JOIN tree ON containers.parent_id = tree.id) '
        'SELECT id FROM tree',
        (container_id,),
    ).fetchall()
    return {row['id'] for row in rows}


def normalize_label(label: str) -> str | None:
    """全角入力や小文字を吸収したラベルIDを返す。空なら None。"""
    normalized: str = unicodedata.normalize('NFKC', label).strip().upper()
    return normalized or None


def suggest_label(conn: sqlite3.Connection, prefix: str = 'A') -> str:
    """接頭辞ごとの次の連番を提案する（例：A-01 の次は A-02）。"""
    normalized_prefix: str = unicodedata.normalize('NFKC', prefix).strip().upper() or 'A'
    rows: list[sqlite3.Row] = conn.execute('SELECT label FROM containers WHERE label IS NOT NULL').fetchall()
    numbers: list[int] = []
    width: int = 2
    for row in rows:
        match: re.Match[str] | None = LABEL_PATTERN.fullmatch(row['label'])
        if match and match.group(1) == normalized_prefix:
            numbers.append(int(match.group(2)))
            width = max(width, len(match.group(2)))
    return f'{normalized_prefix}-{max(numbers, default=0) + 1:0{width}d}'


def _validate(conn: sqlite3.Connection, name: str, kind_id: int, label: str | None, exclude_id: int | None) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not name:
        errors['name'] = '名前を入力してください。'
    if conn.execute('SELECT 1 FROM container_kinds WHERE id = ?', (kind_id,)).fetchone() is None:
        errors['kind_id'] = '種類を選んでください。'
    if label is not None:
        if not LABEL_PATTERN.fullmatch(label):
            errors['label'] = 'ラベルIDは「A-01」のように、英大文字1〜3字・ハイフン・数字2〜4桁で入力してください。'
        elif conn.execute('SELECT 1 FROM containers WHERE label = ? AND id IS NOT ?', (label, exclude_id)).fetchone():
            errors['label'] = 'このラベルIDは使用済みです。'
    return errors


def create_container(conn: sqlite3.Connection, name: str, kind_id: int, parent_id: int | None = None, label: str = '') -> int:
    """保管場所を作成し、変更履歴に記録する。"""
    cleaned: str = clean_text(name)
    normalized_label: str | None = normalize_label(label)
    errors: dict[str, str] = _validate(conn, cleaned, kind_id, normalized_label, None)
    if parent_id is not None:
        parent: Container = get_container(conn, parent_id)
        if parent.retired:
            errors['parent_id'] = '廃止済みの保管場所の中には作成できません。'
    if errors:
        raise ValidationError(errors)
    timestamp: str = now_iso()
    with transaction(conn):
        cursor: sqlite3.Cursor = conn.execute(
            'INSERT INTO containers (name, kind_id, parent_id, label, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)',
            (cleaned, kind_id, parent_id, normalized_label, timestamp, timestamp),
        )
        container_id: int | None = cursor.lastrowid
        assert container_id is not None
        events.record(conn, 'container', container_id, 'create', None, container_snapshot(conn, container_id))
    return container_id


def update_container(conn: sqlite3.Connection, container_id: int, name: str, kind_id: int, label: str = '') -> None:
    """名前・種類・ラベルIDを変更し、変更があれば変更履歴に記録する。"""
    get_container(conn, container_id)
    cleaned: str = clean_text(name)
    normalized_label: str | None = normalize_label(label)
    errors: dict[str, str] = _validate(conn, cleaned, kind_id, normalized_label, container_id)
    if errors:
        raise ValidationError(errors)
    with transaction(conn):
        before: dict[str, object] = container_snapshot(conn, container_id)
        conn.execute(
            'UPDATE containers SET name = ?, kind_id = ?, label = ?, updated_at = ? WHERE id = ?',
            (cleaned, kind_id, normalized_label, now_iso(), container_id),
        )
        after: dict[str, object] = container_snapshot(conn, container_id)
        if before != after:
            events.record(conn, 'container', container_id, 'update', before, after)


def move_container(conn: sqlite3.Connection, container_id: int, parent_id: int | None) -> None:
    """保管場所を別の親の中へ移す。中身の物品ごとの履歴は作らず、保管場所1件の移動として記録する。"""
    container: Container = get_container(conn, container_id)
    if parent_id is not None:
        if parent_id in descendant_ids(conn, container_id):
            raise ValidationError({'parent_id': '自分自身や配下の保管場所の中へは移動できません。'})
        parent: Container = get_container(conn, parent_id)
        if parent.retired:
            raise ValidationError({'parent_id': '廃止済みの保管場所の中へは移動できません。'})
    if container.parent_id == parent_id:
        return
    with transaction(conn):
        before: dict[str, object] = container_snapshot(conn, container_id)
        conn.execute('UPDATE containers SET parent_id = ?, updated_at = ? WHERE id = ?', (parent_id, now_iso(), container_id))
        events.record(conn, 'container', container_id, 'move', before, container_snapshot(conn, container_id))

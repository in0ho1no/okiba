"""物品の登録・内容の修正・移動・検索・箱の中身。"""

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from okiba import catalog, events
from okiba.catalog import INDIVIDUAL_IDENTIFIER_KINDS, Field
from okiba.common import NotFoundError, ValidationError, clean_text, format_number, now_iso, search_normalize
from okiba.containers import Container, all_paths, descendant_ids, get_container
from okiba.db import transaction
from okiba.naming import format_measure, generate_cable_name, is_cable_template
from okiba.snapshots import item_snapshot
from okiba.tags import ensure_tags

STATUS_LABELS: dict[str, str] = {
    'stored': '保管中',
    'in_use': '使用中',
    'lent': '貸出中',
    'sold': '売却済',
    'disposed': '廃棄済',
}

# 所持中の状態。検索の既定の絞り込みに使う。
ACTIVE_STATUSES: tuple[str, ...] = ('stored', 'in_use', 'lent')

# 保管場所の値が戻し先を意味する状態。
RETURNING_STATUSES: frozenset[str] = frozenset({'in_use', 'lent'})

_TRIGRAM_MIN_LENGTH: int = 3


@dataclass
class ItemInput:
    """登録・内容の修正の入力値。属性は内部キーごとの型付きの値、識別コードは種類ごとの値。"""

    category_id: int | None
    name: str
    quantity: int | None
    note: str = ''
    container_id: int | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    identifiers: dict[str, str] = field(default_factory=dict)
    tag_names: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ItemSummary:
    """一覧表示用の物品。"""

    id: int
    name: str
    category_path: str
    quantity: int
    status: str
    location: str
    tags: list[str]

    @property
    def status_label(self) -> str:
        """状態の表示名。"""
        return STATUS_LABELS[self.status]

    @property
    def is_return_target(self) -> bool:
        """所在が戻し先かどうか。"""
        return self.status in RETURNING_STATUSES


@dataclass(frozen=True)
class ItemDetail:
    """詳細画面用の物品。"""

    id: int
    name: str
    category_id: int
    category_path: str
    name_label: str
    quantity: int
    status: str
    container_id: int | None
    location: str
    note: str
    attributes: dict[str, Any]
    identifiers: dict[str, str]
    tags: list[str]
    deleted: bool
    created_at: str
    updated_at: str

    @property
    def status_label(self) -> str:
        """状態の表示名。"""
        return STATUS_LABELS[self.status]

    @property
    def is_return_target(self) -> bool:
        """所在が戻し先かどうか。"""
        return self.status in RETURNING_STATUSES


@dataclass(frozen=True)
class StatusCount:
    """状態ごとの登録件数と数量合計。"""

    status: str
    count: int
    quantity: int

    @property
    def status_label(self) -> str:
        """状態の表示名。"""
        return STATUS_LABELS[self.status]


@dataclass(frozen=True)
class ContainerContents:
    """箱の中身。保管中の物と、この箱を戻し先とする物を分けて持つ。"""

    stored: list[ItemSummary]
    returning: list[ItemSummary]
    counts: list[StatusCount]


@dataclass(frozen=True)
class SearchQuery:
    """検索条件。"""

    text: str = ''
    category_id: int | None = None
    container_id: int | None = None
    tag_id: int | None = None
    statuses: tuple[str, ...] = ACTIVE_STATUSES


def display_value(value: Any) -> str:
    """属性値を表示用の文字列にする。"""
    if isinstance(value, bool):
        return 'はい' if value else 'いいえ'
    if isinstance(value, int | float):
        return format_number(value)
    if isinstance(value, dict):
        return format_measure(value)
    return '' if value is None else str(value)


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _check_input(conn: sqlite3.Connection, data: ItemInput, fields: list[Field], *, require_container: bool) -> dict[str, str]:
    errors: dict[str, str] = {}
    if data.category_id is None:
        errors['category_id'] = 'カテゴリを選んでください。'
    if not data.name:
        errors['name'] = '名称を入力してください。'
    if data.quantity is None or data.quantity < 1:
        errors['quantity'] = '数量は1以上の整数で入力してください。'
    if require_container:
        if data.container_id is None:
            errors['container_id'] = '保管場所を選んでください。'
        else:
            try:
                container: Container = get_container(conn, data.container_id)
            except NotFoundError:
                errors['container_id'] = '保管場所を選んでください。'
            else:
                if container.retired:
                    errors['container_id'] = '廃止済みの保管場所は選べません。'
    for item_field in fields:
        value: Any = data.identifiers.get(item_field.identifier_kind) if item_field.identifier_kind else data.attributes.get(item_field.key)
        if item_field.required and _is_empty(value):
            errors[f'attr__{item_field.key}'] = f'{item_field.label}を入力してください。'
    if any(INDIVIDUAL_IDENTIFIER_KINDS & data.identifiers.keys()) and data.quantity not in (None, 1):
        errors['quantity'] = 'シリアル番号などの個体識別コードがある登録は数量1にしてください。'
    return errors


def _resolve_category(conn: sqlite3.Connection, data: ItemInput) -> list[Field]:
    if data.category_id is None:
        return []
    category: catalog.Category = catalog.get_category(conn, data.category_id)
    if category.retired:
        raise ValidationError({'category_id': '廃止済みのカテゴリは選べません。'})
    return catalog.fields_for_category(conn, data.category_id)


def _clean_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in attributes.items() if not _is_empty(value)}


def _clean_identifiers(identifiers: dict[str, str]) -> dict[str, str]:
    return {kind: value.strip() for kind, value in identifiers.items() if value.strip()}


def _write_relations(conn: sqlite3.Connection, item_id: int, identifiers: dict[str, str], tag_names: list[str]) -> None:
    conn.execute('DELETE FROM identifiers WHERE item_id = ?', (item_id,))
    for kind, value in identifiers.items():
        conn.execute('INSERT INTO identifiers (item_id, kind, value) VALUES (?, ?, ?)', (item_id, kind, value))
    conn.execute('DELETE FROM item_tags WHERE item_id = ?', (item_id,))
    for tag_id in ensure_tags(conn, tag_names):
        conn.execute('INSERT OR IGNORE INTO item_tags (item_id, tag_id) VALUES (?, ?)', (item_id, tag_id))


def _prepare(conn: sqlite3.Connection, data: ItemInput, before: dict[str, Any] | None) -> tuple[ItemInput, dict[str, str]]:
    """入力値を整え（空値の除去・ケーブル類の名称生成）、検証エラーとともに返す。before は修正前の内容（登録時は None）。"""
    try:
        fields: list[Field] = _resolve_category(conn, data)
    except ValidationError as error:
        fields = []
        category_errors: dict[str, str] = error.errors
    except NotFoundError:
        fields = []
        category_errors = {'category_id': 'カテゴリを選んでください。'}
    else:
        category_errors = {}
    attributes: dict[str, Any] = _clean_attributes(data.attributes)
    name: str = clean_text(data.name)
    if is_cable_template(item_field.key for item_field in fields):
        if before is None:
            name = name or generate_cable_name(attributes)
        elif not name or (name == before['name'] and before['name'] == generate_cable_name(before['attributes'])):
            name = generate_cable_name(attributes) or name
    checked: ItemInput = ItemInput(
        category_id=data.category_id,
        name=name,
        quantity=data.quantity,
        note=data.note.strip(),
        container_id=data.container_id if before is None else None,
        attributes=attributes,
        identifiers=_clean_identifiers(data.identifiers),
        tag_names=data.tag_names,
    )
    errors: dict[str, str] = {**_check_input(conn, checked, fields, require_container=before is None), **category_errors}
    return checked, errors


def validate_item(conn: sqlite3.Connection, data: ItemInput, item_id: int | None = None) -> dict[str, str]:
    """保存せずに検証だけを行い、入力欄ごとのエラーを返す。item_id を指定すると内容の修正として検証する。"""
    before: dict[str, Any] | None = item_snapshot(conn, item_id) if item_id is not None else None
    errors: dict[str, str]
    _, errors = _prepare(conn, data, before)
    return errors


def register_item(conn: sqlite3.Connection, data: ItemInput) -> int:
    """物品を登録し、変更履歴に記録する。ケーブル類で名称が空なら属性から生成する。"""
    checked: ItemInput
    errors: dict[str, str]
    checked, errors = _prepare(conn, data, None)
    if errors:
        raise ValidationError(errors)
    timestamp: str = now_iso()
    with transaction(conn):
        cursor: sqlite3.Cursor = conn.execute(
            'INSERT INTO items (name, category_id, quantity, status, container_id, note, attributes, created_at, updated_at) '
            "VALUES (?, ?, ?, 'stored', ?, ?, ?, ?, ?)",
            (
                checked.name,
                checked.category_id,
                checked.quantity,
                checked.container_id,
                checked.note,
                json.dumps(checked.attributes, ensure_ascii=False),
                timestamp,
                timestamp,
            ),
        )
        item_id: int | None = cursor.lastrowid
        assert item_id is not None
        _write_relations(conn, item_id, checked.identifiers, checked.tag_names)
        refresh_search_index(conn, item_id)
        events.record(conn, 'item', item_id, 'create', None, item_snapshot(conn, item_id))
    return item_id


def update_item(conn: sqlite3.Connection, item_id: int, data: ItemInput) -> None:
    """内容（カテゴリ・名称・数量・備考・属性・識別コード・タグ）を修正する。保管場所は移動で扱う。

    ケーブル類の名称は、現在の名称が修正前の属性から生成される名称と同じで、名称欄を変えていない場合だけ再生成する。
    """
    before: dict[str, Any] = item_snapshot(conn, item_id)
    if before['deleted'] or before['merged_into_id'] is not None:
        raise ValidationError({'item': '削除済み・統合済みの登録は修正できません。'})
    checked: ItemInput
    errors: dict[str, str]
    checked, errors = _prepare(conn, data, before)
    if errors:
        raise ValidationError(errors)
    with transaction(conn):
        conn.execute(
            'UPDATE items SET name = ?, category_id = ?, quantity = ?, note = ?, attributes = ?, updated_at = ? WHERE id = ?',
            (
                checked.name,
                checked.category_id,
                checked.quantity,
                checked.note,
                json.dumps(checked.attributes, ensure_ascii=False),
                now_iso(),
                item_id,
            ),
        )
        _write_relations(conn, item_id, checked.identifiers, checked.tag_names)
        refresh_search_index(conn, item_id)
        after: dict[str, Any] = item_snapshot(conn, item_id)
        if after != before:
            events.record(conn, 'item', item_id, 'update', before, after)


def move_item(conn: sqlite3.Connection, item_id: int, container_id: int) -> None:
    """物品を移動する。使用中・貸出中の物は戻し先の変更として扱う。必須項目は検査しない。"""
    before: dict[str, Any] = item_snapshot(conn, item_id)
    if before['deleted'] or before['merged_into_id'] is not None:
        raise ValidationError({'container_id': '削除済み・統合済みの登録は移動できません。'})
    if before['status'] not in ACTIVE_STATUSES:
        raise ValidationError({'container_id': '手放した物は移動できません。'})
    container: Container = get_container(conn, container_id)
    if container.retired:
        raise ValidationError({'container_id': '廃止済みの保管場所は選べません。'})
    if before['container_id'] == container_id:
        return
    with transaction(conn):
        conn.execute('UPDATE items SET container_id = ?, updated_at = ? WHERE id = ?', (container_id, now_iso(), item_id))
        events.record(conn, 'item', item_id, 'move', before, item_snapshot(conn, item_id))


def refresh_search_index(conn: sqlite3.Connection, item_id: int) -> None:
    """全文検索用のテキストを作り直す。属性・識別コード・貸出や手放しの相手と備考を含める。"""
    row: sqlite3.Row | None = conn.execute('SELECT * FROM items WHERE id = ?', (item_id,)).fetchone()
    if row is None:
        raise NotFoundError(f'item {item_id}')
    attributes: dict[str, Any] = json.loads(row['attributes'])
    identifier_rows: list[sqlite3.Row] = conn.execute('SELECT value FROM identifiers WHERE item_id = ?', (item_id,)).fetchall()
    parts: list[str] = [row['name'], row['note'], row['status_party'] or '', row['status_note'] or '']
    parts.extend(display_value(value) for value in attributes.values())
    parts.extend(identifier['value'] for identifier in identifier_rows)
    body: str = search_normalize('\n'.join(part for part in parts if part))
    conn.execute('DELETE FROM item_search WHERE rowid = ?', (item_id,))
    conn.execute('INSERT INTO item_search (rowid, body) VALUES (?, ?)', (item_id, body))


def get_item(conn: sqlite3.Connection, item_id: int) -> ItemDetail:
    """詳細画面用に物品を返す。"""
    snapshot: dict[str, Any] = item_snapshot(conn, item_id)
    row: sqlite3.Row = conn.execute('SELECT created_at, updated_at FROM items WHERE id = ?', (item_id,)).fetchone()
    location: str = _location(conn, snapshot['container_id'])
    return ItemDetail(
        id=snapshot['id'],
        name=snapshot['name'],
        category_id=snapshot['category_id'],
        category_path=catalog.category_path(conn, snapshot['category_id']),
        name_label=catalog.name_label(conn, snapshot['category_id']),
        quantity=snapshot['quantity'],
        status=snapshot['status'],
        container_id=snapshot['container_id'],
        location=location,
        note=snapshot['note'],
        attributes=snapshot['attributes'],
        identifiers={identifier['kind']: identifier['value'] for identifier in snapshot['identifiers']},
        tags=[tag['name'] for tag in snapshot['tags']],
        deleted=snapshot['deleted'],
        created_at=row['created_at'],
        updated_at=row['updated_at'],
    )


def _location(conn: sqlite3.Connection, container_id: int | None) -> str:
    if container_id is None:
        return ''
    paths: dict[int, str] = {container.id: path for container, path in all_paths(conn, include_retired=True)}
    return paths.get(container_id, '')


def _summaries(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[ItemSummary]:
    paths: dict[int, str] = {container.id: path for container, path in all_paths(conn, include_retired=True)}
    category_paths: dict[int, str] = {}
    summaries: list[ItemSummary] = []
    for row in rows:
        category_id: int = row['category_id']
        if category_id not in category_paths:
            category_paths[category_id] = catalog.category_path(conn, category_id)
        tag_rows: list[sqlite3.Row] = conn.execute(
            'SELECT tags.name FROM item_tags JOIN tags ON tags.id = item_tags.tag_id WHERE item_tags.item_id = ? ORDER BY tags.name',
            (row['id'],),
        ).fetchall()
        summaries.append(
            ItemSummary(
                id=row['id'],
                name=row['name'],
                category_path=category_paths[category_id],
                quantity=row['quantity'],
                status=row['status'],
                location=paths.get(row['container_id'], '') if row['container_id'] is not None else '',
                tags=[tag['name'] for tag in tag_rows],
            )
        )
    return summaries


def _text_conditions(text: str) -> tuple[list[str], list[str]]:
    """検索語ごとの条件。3文字以上はFTS5の trigram 索引、2文字以下は LIKE の部分一致で探す。"""
    conditions: list[str] = []
    params: list[str] = []
    for term in search_normalize(text).split():
        if len(term) >= _TRIGRAM_MIN_LENGTH:
            conditions.append('items.id IN (SELECT rowid FROM item_search WHERE item_search MATCH ?)')
            params.append('"' + term.replace('"', '""') + '"')
        else:
            conditions.append("items.id IN (SELECT rowid FROM item_search WHERE body LIKE ? ESCAPE '\\')")
            params.append('%' + re.sub(r'([\\%_])', r'\\\1', term) + '%')
    return conditions, params


def search_items(conn: sqlite3.Connection, query: SearchQuery, limit: int = 500) -> list[ItemSummary]:
    """条件に合う物品を新しい順に返す。削除済み・統合済みの登録は含めない。"""
    conditions: list[str] = ['items.deleted = 0', 'items.merged_into_id IS NULL']
    params: list[Any] = []
    if query.statuses:
        conditions.append(f'items.status IN ({", ".join("?" for _ in query.statuses)})')
        params.extend(query.statuses)
    if query.category_id is not None:
        conditions.append('(items.category_id = ? OR items.category_id IN (SELECT id FROM categories WHERE parent_id = ?))')
        params.extend([query.category_id, query.category_id])
    if query.container_id is not None:
        container_ids: set[int] = descendant_ids(conn, query.container_id)
        conditions.append(f'items.container_id IN ({", ".join("?" for _ in container_ids)})')
        params.extend(sorted(container_ids))
    if query.tag_id is not None:
        conditions.append('EXISTS (SELECT 1 FROM item_tags WHERE item_tags.item_id = items.id AND item_tags.tag_id = ?)')
        params.append(query.tag_id)
    text_conditions: list[str]
    text_params: list[str]
    text_conditions, text_params = _text_conditions(query.text)
    conditions.extend(text_conditions)
    params.extend(text_params)
    params.append(limit)
    rows: list[sqlite3.Row] = conn.execute(
        f'SELECT items.* FROM items WHERE {" AND ".join(conditions)} ORDER BY items.updated_at DESC, items.id DESC LIMIT ?',
        params,
    ).fetchall()
    return _summaries(conn, rows)


def container_contents(conn: sqlite3.Connection, container_id: int, include_nested: bool = False) -> ContainerContents:
    """箱の中身を返す。保管中の物と、使用中・貸出中でこの箱を戻し先とする物を分ける。"""
    get_container(conn, container_id)
    container_ids: list[int] = sorted(descendant_ids(conn, container_id)) if include_nested else [container_id]
    placeholders: str = ', '.join('?' for _ in container_ids)
    rows: list[sqlite3.Row] = conn.execute(
        f'SELECT * FROM items WHERE deleted = 0 AND merged_into_id IS NULL AND container_id IN ({placeholders}) ORDER BY name, id',
        container_ids,
    ).fetchall()
    summaries: list[ItemSummary] = _summaries(conn, rows)
    counts: list[StatusCount] = []
    for status in ACTIVE_STATUSES:
        matched: list[ItemSummary] = [summary for summary in summaries if summary.status == status]
        if matched:
            counts.append(StatusCount(status=status, count=len(matched), quantity=sum(summary.quantity for summary in matched)))
    return ContainerContents(
        stored=[summary for summary in summaries if summary.status == 'stored'],
        returning=[summary for summary in summaries if summary.is_return_target],
        counts=counts,
    )

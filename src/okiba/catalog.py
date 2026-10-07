"""カテゴリとテンプレート（入力項目の定義）。"""

import re
import sqlite3
from dataclasses import dataclass

from okiba.common import NotFoundError, ValidationError, clean_text, now_iso
from okiba.db import transaction

FIELD_TYPES: dict[str, str] = {
    'text': 'テキスト',
    'suggest': '選択候補付きテキスト',
    'number': '数値',
    'measure': '数値＋単位',
    'date': '日付',
    'bool': '真偽値',
}

UNIT_KINDS: dict[str, str] = {'length': '長さ'}

# 単位ごとの基準単位への換算係数。長さの基準単位は mm。
UNITS: dict[str, dict[str, float]] = {'length': {'mm': 1.0, 'cm': 10.0, 'm': 1000.0}}

IDENTIFIER_KINDS: dict[str, str] = {'isbn': 'ISBN', 'jan': 'JAN', 'model': '型番', 'serial': 'シリアル番号'}

# 個体識別のコードは複製・分割で引き継がず、数量1の登録にだけ付けられる。
INDIVIDUAL_IDENTIFIER_KINDS: frozenset[str] = frozenset({'serial'})

_KEY_PATTERN: re.Pattern[str] = re.compile(r'^[a-z][a-z0-9_]*$')


@dataclass(frozen=True)
class Category:
    """カテゴリ。parent_id が None なら大カテゴリ。"""

    id: int
    name: str
    parent_id: int | None
    name_label: str | None
    retired: bool


@dataclass(frozen=True)
class Field:
    """テンプレートの入力項目1件。"""

    id: int
    category_id: int
    key: str
    label: str
    field_type: str
    unit_kind: str | None
    required: bool
    sort_order: int
    clear_on_copy: bool
    dedupe: bool
    identifier_kind: str | None
    retired: bool

    @property
    def type_label(self) -> str:
        """型の表示名。"""
        return FIELD_TYPES[self.field_type]


def _category(row: sqlite3.Row) -> Category:
    return Category(id=row['id'], name=row['name'], parent_id=row['parent_id'], name_label=row['name_label'], retired=bool(row['retired']))


def _field(row: sqlite3.Row) -> Field:
    return Field(
        id=row['id'],
        category_id=row['category_id'],
        key=row['key'],
        label=row['label'],
        field_type=row['field_type'],
        unit_kind=row['unit_kind'],
        required=bool(row['required']),
        sort_order=row['sort_order'],
        clear_on_copy=bool(row['clear_on_copy']),
        dedupe=bool(row['dedupe']),
        identifier_kind=row['identifier_kind'],
        retired=bool(row['retired']),
    )


def list_categories(conn: sqlite3.Connection, include_retired: bool = False) -> list[Category]:
    """カテゴリを大カテゴリ・名前順に返す。"""
    rows: list[sqlite3.Row] = conn.execute('SELECT * FROM categories ORDER BY name').fetchall()
    categories: list[Category] = [_category(row) for row in rows]
    if not include_retired:
        categories = [category for category in categories if not category.retired]
    return categories


def get_category(conn: sqlite3.Connection, category_id: int) -> Category:
    """カテゴリを1件返す。"""
    row: sqlite3.Row | None = conn.execute('SELECT * FROM categories WHERE id = ?', (category_id,)).fetchone()
    if row is None:
        raise NotFoundError(f'category {category_id}')
    return _category(row)


def top_categories(conn: sqlite3.Connection) -> list[Category]:
    """有効な大カテゴリを返す。"""
    return [category for category in list_categories(conn) if category.parent_id is None]


def child_categories(conn: sqlite3.Connection, parent_id: int) -> list[Category]:
    """有効な小カテゴリを返す。"""
    return [category for category in list_categories(conn) if category.parent_id == parent_id]


def category_chain(conn: sqlite3.Connection, category_id: int) -> list[Category]:
    """大カテゴリから順に、指定カテゴリまでの並びを返す。"""
    category: Category = get_category(conn, category_id)
    if category.parent_id is None:
        return [category]
    return [get_category(conn, category.parent_id), category]


def category_path(conn: sqlite3.Connection, category_id: int) -> str:
    """「大カテゴリ › 小カテゴリ」形式の表示名を返す。"""
    return ' › '.join(category.name for category in category_chain(conn, category_id))


def name_label(conn: sqlite3.Connection, category_id: int) -> str:
    """名称欄の表示名（書籍は「タイトル」）を返す。小カテゴリに指定がなければ大カテゴリの指定を使う。"""
    for category in reversed(category_chain(conn, category_id)):
        if category.name_label:
            return category.name_label
    return '名称'


def create_category(conn: sqlite3.Connection, name: str, parent_id: int | None = None) -> int:
    """カテゴリを作成する。カテゴリは大カテゴリ・小カテゴリの2段に限る。"""
    cleaned: str = clean_text(name)
    errors: dict[str, str] = {}
    if not cleaned:
        errors['name'] = 'カテゴリ名を入力してください。'
    if parent_id is not None:
        parent: Category = get_category(conn, parent_id)
        if parent.parent_id is not None:
            errors['parent_id'] = '小カテゴリの下にはカテゴリを作成できません。'
    if not errors and _category_name_exists(conn, cleaned, parent_id):
        errors['name'] = '同じ名前のカテゴリが既にあります。'
    if errors:
        raise ValidationError(errors)
    timestamp: str = now_iso()
    with transaction(conn):
        cursor: sqlite3.Cursor = conn.execute(
            'INSERT INTO categories (name, parent_id, created_at, updated_at) VALUES (?, ?, ?, ?)',
            (cleaned, parent_id, timestamp, timestamp),
        )
    category_id: int | None = cursor.lastrowid
    assert category_id is not None
    return category_id


def _category_name_exists(conn: sqlite3.Connection, name: str, parent_id: int | None, exclude_id: int | None = None) -> bool:
    row: sqlite3.Row | None = conn.execute(
        'SELECT id FROM categories WHERE ifnull(parent_id, 0) = ? AND name = ? AND id IS NOT ?',
        (parent_id or 0, name, exclude_id),
    ).fetchone()
    return row is not None


def rename_category(conn: sqlite3.Connection, category_id: int, name: str) -> None:
    """カテゴリ名を変更する。"""
    category: Category = get_category(conn, category_id)
    cleaned: str = clean_text(name)
    if not cleaned:
        raise ValidationError({'name': 'カテゴリ名を入力してください。'})
    if _category_name_exists(conn, cleaned, category.parent_id, exclude_id=category_id):
        raise ValidationError({'name': '同じ名前のカテゴリが既にあります。'})
    with transaction(conn):
        conn.execute('UPDATE categories SET name = ?, updated_at = ? WHERE id = ?', (cleaned, now_iso(), category_id))


def own_fields(conn: sqlite3.Connection, category_id: int, include_retired: bool = False) -> list[Field]:
    """カテゴリ自身に定義した項目を返す。"""
    rows: list[sqlite3.Row] = conn.execute(
        'SELECT * FROM category_fields WHERE category_id = ? ORDER BY sort_order, id',
        (category_id,),
    ).fetchall()
    return [_field(row) for row in rows if include_retired or not row['retired']]


def fields_for_category(conn: sqlite3.Connection, category_id: int, include_retired: bool = False) -> list[Field]:
    """大カテゴリの項目と小カテゴリの項目を合成し、表示順に並べて返す。"""
    fields: list[Field] = []
    for category in category_chain(conn, category_id):
        fields.extend(own_fields(conn, category.id, include_retired=include_retired))
    return sorted(fields, key=lambda field: field.sort_order)


def get_field(conn: sqlite3.Connection, field_id: int) -> Field:
    """項目定義を1件返す。"""
    row: sqlite3.Row | None = conn.execute('SELECT * FROM category_fields WHERE id = ?', (field_id,)).fetchone()
    if row is None:
        raise NotFoundError(f'field {field_id}')
    return _field(row)


def _related_fields(conn: sqlite3.Connection, category_id: int) -> list[Field]:
    """合成時に同じテンプレートへ並ぶ可能性がある、廃止済みを含む項目を返す。"""
    category: Category = get_category(conn, category_id)
    category_ids: list[int] = [category.id]
    if category.parent_id is None:
        category_ids.extend(child.id for child in list_categories(conn, include_retired=True) if child.parent_id == category.id)
    else:
        category_ids.append(category.parent_id)
    fields: list[Field] = []
    for related_id in category_ids:
        fields.extend(own_fields(conn, related_id, include_retired=True))
    return fields


def add_field(
    conn: sqlite3.Connection,
    category_id: int,
    label: str,
    field_type: str,
    *,
    unit_kind: str | None = None,
    required: bool = False,
    sort_order: int | None = None,
    clear_on_copy: bool = False,
    dedupe: bool = False,
    identifier_kind: str | None = None,
    key: str | None = None,
) -> int:
    """カテゴリに入力項目を追加する。内部キーを省略した場合は `field_<連番>` を割り当てる。"""
    get_category(conn, category_id)
    cleaned_label: str = clean_text(label)
    errors: dict[str, str] = {}
    if not cleaned_label:
        errors['label'] = '表示名を入力してください。'
    if field_type not in FIELD_TYPES:
        errors['field_type'] = '型を選んでください。'
    if field_type == 'measure' and unit_kind not in UNIT_KINDS:
        errors['unit_kind'] = '数値＋単位の項目は単位の種類を選んでください。'
    if field_type != 'measure':
        unit_kind = None
    if identifier_kind is not None:
        if identifier_kind not in IDENTIFIER_KINDS:
            errors['identifier_kind'] = '識別子の種類が正しくありません。'
        elif field_type != 'text':
            errors['identifier_kind'] = '識別コードの項目はテキスト型にしてください。'
    related: list[Field] = _related_fields(conn, category_id)
    if key is None or not key.strip():
        key = _next_key(related)
    key = key.strip()
    if not _KEY_PATTERN.fullmatch(key):
        errors['key'] = '内部キーは英小文字で始まる英小文字・数字・_ で入力してください。'
    elif any(field.key == key for field in related):
        errors['key'] = 'この内部キーは大カテゴリ・小カテゴリのどこかで使用済みです（廃止済みの項目を含む）。'
    if identifier_kind is not None and any(field.identifier_kind == identifier_kind and not field.retired for field in related):
        errors['identifier_kind'] = 'この識別子の種類の項目は既にあります。'
    if errors:
        raise ValidationError(errors)
    if sort_order is None:
        sort_order = max((field.sort_order for field in related), default=0) + 10
    timestamp: str = now_iso()
    with transaction(conn):
        cursor: sqlite3.Cursor = conn.execute(
            'INSERT INTO category_fields (category_id, key, label, field_type, unit_kind, required, sort_order, clear_on_copy, dedupe, '
            'identifier_kind, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (
                category_id,
                key,
                cleaned_label,
                field_type,
                unit_kind,
                int(required),
                sort_order,
                int(clear_on_copy),
                int(dedupe),
                identifier_kind,
                timestamp,
                timestamp,
            ),
        )
    field_id: int | None = cursor.lastrowid
    assert field_id is not None
    return field_id


def _next_key(related: list[Field]) -> str:
    numbers: list[int] = [int(match.group(1)) for field in related if (match := re.fullmatch(r'field_(\d+)', field.key))]
    return f'field_{max(numbers, default=0) + 1}'


def update_field(
    conn: sqlite3.Connection,
    field_id: int,
    *,
    label: str,
    sort_order: int,
    required: bool,
    clear_on_copy: bool,
    dedupe: bool,
) -> None:
    """変更可能な設定だけを更新する。内部キー・型・識別子の種類は作成後に変更できない。"""
    get_field(conn, field_id)
    cleaned_label: str = clean_text(label)
    if not cleaned_label:
        raise ValidationError({'label': '表示名を入力してください。'})
    with transaction(conn):
        conn.execute(
            'UPDATE category_fields SET label = ?, sort_order = ?, required = ?, clear_on_copy = ?, dedupe = ?, updated_at = ? WHERE id = ?',
            (cleaned_label, sort_order, int(required), int(clear_on_copy), int(dedupe), now_iso(), field_id),
        )


def suggestions(conn: sqlite3.Connection, key: str) -> list[str]:
    """選択候補付きテキストの候補として、過去に入力された値を返す。"""
    if not _KEY_PATTERN.fullmatch(key):
        return []
    rows: list[sqlite3.Row] = conn.execute(
        'SELECT DISTINCT json_extract(attributes, ?) AS value FROM items WHERE json_type(attributes, ?) = ? ORDER BY value',
        (f'$.{key}', f'$.{key}', 'text'),
    ).fetchall()
    return [row['value'] for row in rows]

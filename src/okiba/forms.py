"""物品の入力フォームの状態と、型付きの入力値への変換。

フォームの状態は文字列のまま保持し、検証エラーで再表示しても入力内容が失われないようにする。
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from okiba.catalog import INDIVIDUAL_IDENTIFIER_KINDS, UNITS, Field
from okiba.items import ItemDetail, ItemInput, display_value
from okiba.tags import parse_tag_text

ATTR_PREFIX: str = 'attr__'
EXTRA_PREFIX: str = 'extra__'
UNIT_SUFFIX: str = '__unit'

# テンプレートに保存しない「項目を追加」の内部キー。テンプレートの内部キー（英小文字始まり）とは衝突しない。
ADHOC_PREFIX: str = 'x:'


@dataclass
class ItemForm:
    """物品の入力フォームの状態。"""

    major_id: int | None = None
    minor_id: int | None = None
    name: str = ''
    container_id: str = ''
    quantity: str = '1'
    note: str = ''
    tags: str = ''
    values: dict[str, str] = field(default_factory=dict)
    extras: dict[str, str] = field(default_factory=dict)

    @property
    def category_id(self) -> int | None:
        """選択中のカテゴリ。小カテゴリがあれば小カテゴリ。"""
        return self.minor_id if self.minor_id is not None else self.major_id

    def unit(self, key: str) -> str:
        """数値＋単位の項目で選択中の単位。"""
        return self.values.get(f'{key}{UNIT_SUFFIX}', '')


def _optional_int(raw: str | None) -> int | None:
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def form_from_mapping(data: Mapping[str, Any]) -> ItemForm:
    """送信されたフォームの値からフォームの状態を作る。ファイルなど文字列以外の値は無視する。"""
    texts: dict[str, str] = {key: value for key, value in data.items() if isinstance(value, str)}
    values: dict[str, str] = {key.removeprefix(ATTR_PREFIX): value for key, value in texts.items() if key.startswith(ATTR_PREFIX)}
    extras: dict[str, str] = {key.removeprefix(EXTRA_PREFIX): value for key, value in texts.items() if key.startswith(EXTRA_PREFIX)}
    return ItemForm(
        major_id=_optional_int(texts.get('major_id')),
        minor_id=_optional_int(texts.get('minor_id')),
        name=texts.get('name', ''),
        container_id=texts.get('container_id', ''),
        quantity=texts.get('quantity', '1'),
        note=texts.get('note', ''),
        tags=texts.get('tags', ''),
        values=values,
        extras=extras,
    )


def _parse_number(raw: str) -> int | float | None:
    try:
        number: float = float(raw)
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def _parse_field(item_field: Field, raw: str, unit: str) -> tuple[Any, str | None]:
    """型に応じて入力値を解釈する。戻り値は (値, エラーメッセージ)。未入力は値 None。"""
    text: str = raw.strip()
    if not text:
        return None, None
    if item_field.field_type in ('text', 'suggest'):
        return text, None
    if item_field.field_type == 'number':
        number: int | float | None = _parse_number(text)
        return (number, None) if number is not None else (None, f'{item_field.label}は数値で入力してください。')
    if item_field.field_type == 'measure':
        measure: int | float | None = _parse_number(text)
        if measure is None or measure < 0:
            return None, f'{item_field.label}は0以上の数値で入力してください。'
        units: dict[str, float] = UNITS.get(item_field.unit_kind or '', {})
        if unit not in units:
            return None, f'{item_field.label}の単位を選んでください。'
        return {'value': measure, 'unit': unit}, None
    if item_field.field_type == 'date':
        try:
            return date.fromisoformat(text).isoformat(), None
        except ValueError:
            return None, f'{item_field.label}は日付で入力してください。'
    if item_field.field_type == 'bool':
        if text in ('true', 'false'):
            return text == 'true', None
        return None, f'{item_field.label}は「はい」「いいえ」から選んでください。'
    return None, f'{item_field.label}の型が不明です。'


def form_to_input(
    form: ItemForm,
    fields: list[Field],
    existing_attributes: Mapping[str, Any] | None = None,
    existing_identifiers: Mapping[str, str] | None = None,
) -> tuple[ItemInput, dict[str, str]]:
    """フォームの状態を登録用の入力値に変換する。

    テンプレート外の項目は、表示文字列が変わっていなければ元の型付きの値を保ち、変わっていれば文字列として保存する。
    テンプレートに入力欄のない種類の識別コードは、属性に移さず既存の値をそのまま残す。
    """
    errors: dict[str, str] = {}
    attributes: dict[str, Any] = {}
    identifiers: dict[str, str] = {}
    for item_field in fields:
        raw: str = form.values.get(item_field.key, '')
        if item_field.identifier_kind:
            if raw.strip():
                identifiers[item_field.identifier_kind] = raw.strip()
            continue
        value: Any
        error: str | None
        value, error = _parse_field(item_field, raw, form.unit(item_field.key))
        if error:
            errors[f'{ATTR_PREFIX}{item_field.key}'] = error
        elif value is not None:
            attributes[item_field.key] = value
    field_keys: set[str] = {item_field.key for item_field in fields}
    existing: Mapping[str, Any] = existing_attributes or {}
    for key, raw in form.extras.items():
        text: str = raw.strip()
        if key in field_keys or not text:
            continue
        attributes[key] = existing[key] if key in existing and display_value(existing[key]) == text else text
    field_kinds: set[str] = {item_field.identifier_kind for item_field in fields if item_field.identifier_kind}
    for kind, value in (existing_identifiers or {}).items():
        if kind not in field_kinds:
            identifiers[kind] = value
    quantity: int | None = _optional_int(form.quantity)
    if quantity is None and form.quantity.strip():
        errors['quantity'] = '数量は1以上の整数で入力してください。'
    data: ItemInput = ItemInput(
        category_id=form.category_id,
        name=form.name,
        quantity=quantity,
        note=form.note,
        container_id=_optional_int(form.container_id),
        attributes=attributes,
        identifiers=identifiers,
        tag_names=parse_tag_text(form.tags),
    )
    return data, errors


def _raw_values(attributes: Mapping[str, Any], identifiers: Mapping[str, str], fields: list[Field]) -> tuple[dict[str, str], dict[str, str]]:
    values: dict[str, str] = {}
    for item_field in fields:
        if item_field.identifier_kind:
            if item_field.identifier_kind in identifiers:
                values[item_field.key] = identifiers[item_field.identifier_kind]
            continue
        value: Any = attributes.get(item_field.key)
        if value is None:
            continue
        if item_field.field_type == 'measure' and isinstance(value, Mapping):
            values[item_field.key] = display_value(value.get('value'))
            values[f'{item_field.key}{UNIT_SUFFIX}'] = str(value.get('unit', ''))
        elif item_field.field_type == 'bool' and isinstance(value, bool):
            values[item_field.key] = 'true' if value else 'false'
        else:
            values[item_field.key] = display_value(value)
    field_keys: set[str] = {item_field.key for item_field in fields}
    extras: dict[str, str] = {key: display_value(value) for key, value in attributes.items() if key not in field_keys}
    return values, extras


def form_from_item(item: ItemDetail, major_id: int, minor_id: int | None, fields: list[Field]) -> ItemForm:
    """登録済みの物品から編集フォームの状態を作る。"""
    values: dict[str, str]
    extras: dict[str, str]
    values, extras = _raw_values(item.attributes, item.identifiers, fields)
    return ItemForm(
        major_id=major_id,
        minor_id=minor_id,
        name=item.name,
        container_id=str(item.container_id or ''),
        quantity=str(item.quantity),
        note=item.note,
        tags=', '.join(item.tags),
        values=values,
        extras=extras,
    )


def copy_form(item: ItemDetail, major_id: int, minor_id: int | None, fields: list[Field]) -> ItemForm:
    """複製登録用のフォームを作る。複製時にクリアする項目と個体識別のコードは空にする。写真は引き継がない。"""
    form: ItemForm = form_from_item(item, major_id, minor_id, fields)
    for item_field in fields:
        if item_field.clear_on_copy or (item_field.identifier_kind in INDIVIDUAL_IDENTIFIER_KINDS):
            form.values.pop(item_field.key, None)
            form.values.pop(f'{item_field.key}{UNIT_SUFFIX}', None)
    return form


def move_unmatched_to_extras(form: ItemForm, old_fields: list[Field], new_fields: list[Field]) -> None:
    """カテゴリ変更後のテンプレートにない入力値を、テンプレート外の項目として残す。識別コードは属性に移さない。"""
    field_keys: set[str] = {item_field.key for item_field in new_fields}
    identifier_keys: set[str] = {item_field.key for item_field in old_fields if item_field.identifier_kind}
    for key in list(form.values):
        if key.endswith(UNIT_SUFFIX) or key in identifier_keys:
            continue
        raw: str = form.values[key].strip()
        if key in field_keys or not raw:
            continue
        unit: str = form.values.pop(f'{key}{UNIT_SUFFIX}', '')
        form.extras.setdefault(key, f'{raw}{unit}')
        del form.values[key]

"""履歴からの差し戻し。誤って上書きした名称・属性・タグ・備考を、履歴の変更前の内容に戻す。"""

import sqlite3
from dataclasses import dataclass
from typing import Any

from okiba import catalog, items
from okiba.common import NotFoundError, ValidationError
from okiba.events import Event, get_event
from okiba.forms import ADHOC_PREFIX
from okiba.items import ItemInput, display_value
from okiba.snapshots import item_snapshot


@dataclass(frozen=True)
class RevertChange:
    """差し戻しで変わる項目の、現在と差し戻し後の表示値。"""

    label: str
    current: str
    reverted: str


@dataclass(frozen=True)
class RevertPlan:
    """差し戻しの内容。data は保存する入力値、retired_tags は差し戻すタグのうち廃止済みのもの。"""

    event: Event
    data: ItemInput
    changes: list[RevertChange]
    retired_tags: list[str]
    errors: dict[str, str]


def _resolve_tag(conn: sqlite3.Connection, tag: dict[str, Any]) -> tuple[str, bool]:
    """履歴のタグを、統合済みなら統合先に読み替えて（名前, 廃止済みか）を返す。"""
    tag_id: int | None = tag['id']
    seen: set[int] = set()
    row: sqlite3.Row | None = None
    while tag_id is not None and tag_id not in seen:
        seen.add(tag_id)
        row = conn.execute('SELECT id, name, retired, merged_into_id FROM tags WHERE id = ?', (tag_id,)).fetchone()
        if row is None:
            break
        tag_id = row['merged_into_id']
    if row is None:
        return tag['name'], False
    return row['name'], bool(row['retired'])


def _attribute_labels(conn: sqlite3.Connection, category_id: int, keys: set[str]) -> list[tuple[str, str]]:
    """属性の（内部キー, 表示名）を、テンプレートの表示順、テンプレート外の項目の順で返す。"""
    fields: list[catalog.Field] = catalog.fields_for_category(conn, category_id, include_retired=True)
    ordered: list[tuple[str, str]] = [(field.key, field.label) for field in fields if field.key in keys]
    known: set[str] = {key for key, _ in ordered}
    for key in sorted(keys - known):
        ordered.append((key, key.removeprefix(ADHOC_PREFIX)))
    return ordered


def _changes(conn: sqlite3.Connection, current: dict[str, Any], data: ItemInput, current_tags: list[str]) -> list[RevertChange]:
    changes: list[RevertChange] = []
    if current['name'] != data.name:
        changes.append(RevertChange('名称', current['name'], data.name))
    keys: set[str] = set(current['attributes']) | set(data.attributes)
    for key, label in _attribute_labels(conn, current['category_id'], keys):
        now: Any = current['attributes'].get(key)
        then: Any = data.attributes.get(key)
        if now != then:
            changes.append(RevertChange(label, display_value(now), display_value(then)))
    if sorted(current_tags) != sorted(data.tag_names):
        changes.append(RevertChange('タグ', '、'.join(current_tags), '、'.join(data.tag_names)))
    if current['note'] != data.note:
        changes.append(RevertChange('備考', current['note'], data.note))
    return changes


def plan_revert(conn: sqlite3.Connection, item_id: int, event_id: int) -> RevertPlan:
    """履歴1件の変更前にある名称・属性・タグ・備考へ戻す内容と、現在との差分を返す。保存はしない。"""
    event: Event = get_event(conn, event_id)
    if event.target_type != 'item' or event.target_id != item_id:
        raise NotFoundError(f'event {event_id} of item {item_id}')
    if event.before is None:
        raise ValidationError({'item': 'この履歴には差し戻せる変更前の内容がありません。'})
    current: dict[str, Any] = item_snapshot(conn, item_id)
    tag_names: list[str] = []
    retired_tags: list[str] = []
    for tag in event.before['tags']:
        name: str
        retired: bool
        name, retired = _resolve_tag(conn, tag)
        if name not in tag_names:
            tag_names.append(name)
            if retired:
                retired_tags.append(name)
    requested: ItemInput = ItemInput(
        category_id=current['category_id'],
        name=event.before['name'],
        quantity=current['quantity'],
        note=event.before['note'],
        attributes=dict(event.before['attributes']),
        identifiers={identifier['kind']: identifier['value'] for identifier in current['identifiers']},
        tag_names=tag_names,
    )
    data: ItemInput
    errors: dict[str, str]
    data, errors = items.prepare_item(conn, requested, item_id)
    current_tags: list[str] = [tag['name'] for tag in current['tags']]
    return RevertPlan(event=event, data=data, changes=_changes(conn, current, data, current_tags), retired_tags=retired_tags, errors=errors)


def apply_revert(conn: sqlite3.Connection, item_id: int, event_id: int) -> bool:
    """差し戻しを新しい修正として適用し、変更があったかを返す。後の履歴は書き換えない。"""
    plan: RevertPlan = plan_revert(conn, item_id, event_id)
    if plan.errors:
        raise ValidationError(plan.errors)
    memo: str = f'履歴からの差し戻し（{plan.event.created_at}の{plan.event.label}の変更前）'
    return items.update_item(conn, item_id, plan.data, memo)

"""ケーブル類の固定処理のうち、名称の自動生成。

フェーズ1ではカテゴリ名に依存させず、内部キーで対象を判定する。
"""

from collections.abc import Iterable, Mapping
from typing import Any

from okiba.common import format_number

CABLE_KEYS: frozenset[str] = frozenset({'connector_a', 'connector_b', 'length'})


def is_cable_template(keys: Iterable[str]) -> bool:
    """ケーブル類の固定処理を適用するテンプレートか判定する。"""
    return set(keys) >= CABLE_KEYS


def format_measure(value: Any) -> str:
    """数値＋単位の値を「1.8m」の形にする。形式が不正なら空文字を返す。"""
    if not isinstance(value, Mapping):
        return ''
    number: Any = value.get('value')
    unit: Any = value.get('unit')
    if not isinstance(number, int | float) or not isinstance(unit, str):
        return ''
    return f'{format_number(number)}{unit}'


def generate_cable_name(attributes: Mapping[str, Any]) -> str:
    """端子A・端子B・長さから名称を生成する（例：`HDMI - DisplayPort 1.8m`）。端子がなければ空文字を返す。"""
    connectors: list[str] = [str(attributes[key]) for key in ('connector_a', 'connector_b') if attributes.get(key)]
    if not connectors:
        return ''
    length: str = format_measure(attributes.get('length'))
    name: str = ' - '.join(connectors)
    return f'{name} {length}' if length else name

"""テストデータを作る補助関数。"""

import io
import sqlite3
from dataclasses import dataclass
from typing import Any

from PIL import Image

import okiba.photos  # noqa: F401  HEIF形式の保存に pillow-heif のプラグイン登録が必要
from okiba import containers, items, status
from okiba.status import StatusRecord

BOOKS_ID: int = 1
CABLES_ID: int = 2

KIND_ROOM: int = 1
KIND_FURNITURE: int = 2
KIND_SHELF: int = 3
KIND_BOX: int = 4


@dataclass(frozen=True)
class Places:
    """テスト用の保管場所のID。"""

    study: int
    bookcase: int
    upper_shelf: int
    box_a1: int
    box_a2: int
    closet: int
    box_b1: int


def make_places(conn: sqlite3.Connection) -> Places:
    """階層つきの保管場所を作る。"""
    study: int = containers.create_container(conn, '書斎', KIND_ROOM)
    bookcase: int = containers.create_container(conn, '本棚', KIND_FURNITURE, study)
    upper_shelf: int = containers.create_container(conn, '上段', KIND_SHELF, bookcase)
    box_a1: int = containers.create_container(conn, '箱', KIND_BOX, upper_shelf, 'A-01')
    box_a2: int = containers.create_container(conn, '箱', KIND_BOX, upper_shelf, 'A-02')
    closet: int = containers.create_container(conn, '押入れ', KIND_FURNITURE)
    box_b1: int = containers.create_container(conn, '箱', KIND_BOX, closet, 'B-01')
    return Places(study, bookcase, upper_shelf, box_a1, box_a2, closet, box_b1)


def cable_input(
    container_id: int, a: str = 'HDMI', b: str = 'DisplayPort', length: float | None = 1.8, unit: str = 'm', **extra: Any
) -> items.ItemInput:
    """ケーブル類の入力値。名称は空（自動生成）。"""
    attributes: dict[str, Any] = {'connector_a': a, 'connector_b': b}
    if length is not None:
        attributes['length'] = {'value': length, 'unit': unit}
    data: items.ItemInput = items.ItemInput(category_id=CABLES_ID, name='', quantity=1, container_id=container_id, attributes=attributes)
    for key, value in extra.items():
        setattr(data, key, value)
    return data


def book_input(container_id: int, title: str = 'リーダブルコード', isbn: str = '9784873115658', **extra: Any) -> items.ItemInput:
    """書籍の入力値。"""
    data: items.ItemInput = items.ItemInput(
        category_id=BOOKS_ID,
        name=title,
        quantity=1,
        container_id=container_id,
        attributes={'author': 'Dustin Boswell', 'publisher': 'オライリー・ジャパン'},
        identifiers={'isbn': isbn} if isbn else {},
    )
    for key, value in extra.items():
        setattr(data, key, value)
    return data


def set_status(conn: sqlite3.Connection, item_id: int, target: str) -> None:
    """保管中の物を、状態の操作（取り出す・貸出・売却・廃棄）で指定の状態にする。"""
    if target == 'in_use':
        status.take_out(conn, item_id)
    elif target == 'lent':
        status.lend(conn, item_id, StatusRecord(date='2026-10-01', party='友人'))
    else:
        status.release(conn, item_id, target, StatusRecord(date='2026-10-01', party='古書店'))


def image_bytes(
    size: tuple[int, int] = (400, 200), image_format: str = 'JPEG', orientation: int | None = None, color: tuple[int, int, int] = (200, 40, 40)
) -> bytes:
    """単色の画像ファイルの中身を作る。orientation を指定するとEXIFの回転情報を付ける。"""
    image: Image.Image = Image.new('RGB', size, color)
    buffer: io.BytesIO = io.BytesIO()
    if orientation is not None:
        exif: Image.Exif = Image.Exif()
        exif[0x0112] = orientation
        image.save(buffer, format=image_format, exif=exif.tobytes())
    else:
        image.save(buffer, format=image_format)
    return buffer.getvalue()


def retire_container(conn: sqlite3.Connection, container_id: int) -> None:
    """保管場所の廃止はフェーズ1-4で実装するため、DBを直接書き換えて廃止状態を用意する。"""
    conn.execute('UPDATE containers SET retired = 1 WHERE id = ?', (container_id,))

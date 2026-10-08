"""写真の取り込み・閲覧用画像の生成・紐付けの管理。"""

import hashlib
import io
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pillow_heif
from PIL import Image, ImageOps, UnidentifiedImageError

from okiba import events
from okiba.common import NotFoundError, ValidationError, clean_text, now_iso
from okiba.db import transaction
from okiba.events import TargetType
from okiba.snapshots import container_snapshot, item_snapshot

pillow_heif.register_heif_opener()

DISPLAY_MAX_SIZE: int = 1600
THUMB_MAX_SIZE: int = 320
JPEG_QUALITY: int = 85

PURPOSE_SUGGESTIONS: tuple[str, ...] = ('表紙', '裏表紙', '正面', '裏面', '箱の中身全体', 'その他')

_FORMAT_EXTENSIONS: dict[str, str] = {
    'JPEG': 'jpg',
    'MPO': 'jpg',
    'PNG': 'png',
    'HEIF': 'heic',
    'WEBP': 'webp',
    'GIF': 'gif',
}


# 紐付け先の種類ごとの一覧取得SQL。列名を文字列で組み立てず、固定のSQLから選ぶ。
_LIST_SQL: dict[str, str] = {
    'item': 'SELECT * FROM photos WHERE item_id = ? ORDER BY id',
    'container': 'SELECT * FROM photos WHERE container_id = ? ORDER BY id',
}


@dataclass(frozen=True)
class Photo:
    """取り込み済みの写真。パスは写真フォルダからの相対パス（区切りは /）。"""

    id: int
    sha256: str
    original_path: str
    display_path: str
    thumb_path: str
    original_name: str
    purpose: str
    item_id: int | None
    container_id: int | None
    detached: bool
    created_at: str


@dataclass(frozen=True)
class ImportResult:
    """取り込み結果。duplicates は同じ内容の写真が既に紐付いている先の説明。"""

    photo_id: int
    duplicates: list[str]


def _photo(row: sqlite3.Row) -> Photo:
    return Photo(
        id=row['id'],
        sha256=row['sha256'],
        original_path=row['original_path'],
        display_path=row['display_path'],
        thumb_path=row['thumb_path'],
        original_name=row['original_name'],
        purpose=row['purpose'],
        item_id=row['item_id'],
        container_id=row['container_id'],
        detached=bool(row['detached']),
        created_at=row['created_at'],
    )


def _flatten(image: Image.Image) -> Image.Image:
    """透過を白背景に合成し、JPEGで保存できるRGB画像にする。"""
    oriented: Image.Image = ImageOps.exif_transpose(image)
    if oriented.mode in ('RGBA', 'LA', 'P'):
        rgba: Image.Image = oriented.convert('RGBA')
        background: Image.Image = Image.new('RGB', rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel('A'))
        return background
    return oriented.convert('RGB')


def _save_resized(image: Image.Image, max_size: int, path: Path) -> None:
    resized: Image.Image = image.copy()
    resized.thumbnail((max_size, max_size))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path = path.with_suffix('.tmp')
    resized.save(temporary, format='JPEG', quality=JPEG_QUALITY)
    temporary.replace(path)


def _describe_target(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    if row['item_id'] is not None:
        item: sqlite3.Row = conn.execute('SELECT name FROM items WHERE id = ?', (row['item_id'],)).fetchone()
        return f'物品「{item["name"]}」'
    container: sqlite3.Row = conn.execute('SELECT name FROM containers WHERE id = ?', (row['container_id'],)).fetchone()
    return f'保管場所「{container["name"]}」'


def _target(item_id: int | None, container_id: int | None) -> tuple[TargetType, int]:
    if (item_id is None) == (container_id is None):
        raise ValueError('item_id と container_id のどちらか一方を指定してください。')
    if item_id is not None:
        return 'item', item_id
    assert container_id is not None
    return 'container', container_id


def _snapshot(conn: sqlite3.Connection, target_type: TargetType, target_id: int) -> dict[str, Any]:
    return item_snapshot(conn, target_id) if target_type == 'item' else container_snapshot(conn, target_id)


def import_photo(
    conn: sqlite3.Connection,
    photos_dir: Path,
    content: bytes,
    filename: str,
    purpose: str = '',
    *,
    item_id: int | None = None,
    container_id: int | None = None,
) -> ImportResult:
    """写真を写真フォルダへコピーし、閲覧用画像と縮小版を生成して紐付ける。

    ファイルは内容のハッシュで名付けるため、同じ写真を再度取り込んでも画像ファイルは増えない。
    """
    target_type: TargetType
    target_id: int
    target_type, target_id = _target(item_id, container_id)
    snapshot_before: dict[str, Any] = _snapshot(conn, target_type, target_id)
    if snapshot_before.get('deleted'):
        raise ValidationError({'photo': '削除済みの登録には写真を追加できません。'})
    try:
        image: Image.Image = Image.open(io.BytesIO(content))
        image.load()
    except (UnidentifiedImageError, OSError) as error:
        raise ValidationError({'photo': '画像として読み込めないファイルです。'}) from error
    extension: str | None = _FORMAT_EXTENSIONS.get(image.format or '')
    if extension is None:
        raise ValidationError({'photo': f'対応していない画像形式です（{image.format}）。'})
    sha256: str = hashlib.sha256(content).hexdigest()
    duplicates: list[str] = [
        _describe_target(conn, row) for row in conn.execute('SELECT item_id, container_id FROM photos WHERE sha256 = ? ORDER BY id', (sha256,))
    ]
    original_path: str = f'originals/{sha256[:2]}/{sha256}.{extension}'
    display_path: str = f'display/{sha256[:2]}/{sha256}.jpg'
    thumb_path: str = f'thumbs/{sha256[:2]}/{sha256}.jpg'
    original_file: Path = photos_dir / original_path
    if not original_file.exists():
        original_file.parent.mkdir(parents=True, exist_ok=True)
        original_file.write_bytes(content)
    if not (photos_dir / display_path).exists() or not (photos_dir / thumb_path).exists():
        flattened: Image.Image = _flatten(image)
        _save_resized(flattened, DISPLAY_MAX_SIZE, photos_dir / display_path)
        _save_resized(flattened, THUMB_MAX_SIZE, photos_dir / thumb_path)
    with transaction(conn):
        cursor: sqlite3.Cursor = conn.execute(
            'INSERT INTO photos (sha256, original_path, display_path, thumb_path, original_name, purpose, item_id, container_id, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (sha256, original_path, display_path, thumb_path, Path(filename).name, clean_text(purpose), item_id, container_id, now_iso()),
        )
        photo_id: int | None = cursor.lastrowid
        assert photo_id is not None
        events.record(conn, target_type, target_id, 'update', snapshot_before, _snapshot(conn, target_type, target_id), memo='写真の紐付け')
    return ImportResult(photo_id=photo_id, duplicates=duplicates)


def get_photo(conn: sqlite3.Connection, photo_id: int) -> Photo:
    """写真を1件返す。"""
    row: sqlite3.Row | None = conn.execute('SELECT * FROM photos WHERE id = ?', (photo_id,)).fetchone()
    if row is None:
        raise NotFoundError(f'photo {photo_id}')
    return _photo(row)


def list_photos(
    conn: sqlite3.Connection, *, item_id: int | None = None, container_id: int | None = None, include_detached: bool = False
) -> list[Photo]:
    """紐付いている写真を取り込み順に返す。"""
    target_type: TargetType
    target_id: int
    target_type, target_id = _target(item_id, container_id)
    rows: list[sqlite3.Row] = conn.execute(_LIST_SQL[target_type], (target_id,)).fetchall()
    return [_photo(row) for row in rows if include_detached or not row['detached']]


def _update_photo(conn: sqlite3.Connection, photo_id: int, sql: str, params: tuple[Any, ...], memo: str) -> None:
    photo: Photo = get_photo(conn, photo_id)
    target_type: TargetType
    target_id: int
    target_type, target_id = _target(photo.item_id, photo.container_id)
    with transaction(conn):
        before: dict[str, Any] = _snapshot(conn, target_type, target_id)
        conn.execute(sql, (*params, photo_id))
        after: dict[str, Any] = _snapshot(conn, target_type, target_id)
        if before != after:
            events.record(conn, target_type, target_id, 'update', before, after, memo=memo)


def detach_photo(conn: sqlite3.Connection, photo_id: int) -> None:
    """紐付けを解除する。画像ファイルは削除せず、変更履歴から辿れるようにする。"""
    _update_photo(conn, photo_id, 'UPDATE photos SET detached = 1 WHERE id = ?', (), '写真の紐付け解除')


def set_purpose(conn: sqlite3.Connection, photo_id: int, purpose: str) -> None:
    """写真の用途を変更する。"""
    _update_photo(conn, photo_id, 'UPDATE photos SET purpose = ? WHERE id = ?', (clean_text(purpose),), '写真の用途変更')


def regenerate_missing_images(conn: sqlite3.Connection, photos_dir: Path) -> int:
    """閲覧用画像・縮小版が見つからない写真を元画像から作り直し、作り直した件数を返す。

    閲覧用画像を含まないバックアップから復元しても表示できるよう、起動時に呼ぶ。読み込めない元画像は飛ばす。
    """
    regenerated: int = 0
    for row in conn.execute('SELECT original_path, display_path, thumb_path FROM photos ORDER BY id').fetchall():
        display_file: Path = photos_dir / row['display_path']
        thumb_file: Path = photos_dir / row['thumb_path']
        original_file: Path = photos_dir / row['original_path']
        if (display_file.exists() and thumb_file.exists()) or not original_file.exists():
            continue
        try:
            with Image.open(original_file) as image:
                image.load()
                flattened: Image.Image = _flatten(image)
        except (UnidentifiedImageError, OSError):
            continue
        _save_resized(flattened, DISPLAY_MAX_SIZE, display_file)
        _save_resized(flattened, THUMB_MAX_SIZE, thumb_file)
        regenerated += 1
    return regenerated

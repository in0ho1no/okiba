"""写真の取り込み・閲覧用画像の生成・紐付けのテスト。"""

import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from okiba import events, items, photos
from okiba.common import ValidationError
from okiba.config import Settings
from okiba.photos import ImportResult, Photo
from tests.helpers import Places, book_input, image_bytes


@pytest.fixture
def item_id(conn: sqlite3.Connection, places: Places) -> int:
    """写真を紐付ける書籍。"""
    return items.register_item(conn, book_input(places.box_a1))


def _size(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        return image.size


class TestImport:
    def test_jpeg_is_copied_and_resized_with_orientation(self, conn: sqlite3.Connection, settings: Settings, item_id: int) -> None:
        """前提: 横長でEXIFに90度回転の指定がある写真 / 操作: 取り込む / 期待: 元ファイルはそのまま、閲覧用画像と縮小版は縦長になる。"""
        content: bytes = image_bytes((400, 200), orientation=6)
        result: ImportResult = photos.import_photo(conn, settings.photos_dir, content, 'IMG_0001.JPG', '表紙', item_id=item_id)
        photo: Photo = photos.get_photo(conn, result.photo_id)
        assert (settings.photos_dir / photo.original_path).read_bytes() == content
        assert photo.original_path.endswith('.jpg')
        assert _size(settings.photos_dir / photo.display_path) == (200, 400)
        thumb_width: int
        thumb_height: int
        thumb_width, thumb_height = _size(settings.photos_dir / photo.thumb_path)
        assert max(thumb_width, thumb_height) <= photos.THUMB_MAX_SIZE
        assert thumb_height > thumb_width
        assert (photo.purpose, photo.original_name, result.duplicates) == ('表紙', 'IMG_0001.JPG', [])

    def test_heic_from_iphone_is_supported(self, conn: sqlite3.Connection, settings: Settings, item_id: int) -> None:
        """前提: HEIC形式の写真 / 操作: 取り込む / 期待: 元ファイルは .heic で保存され、ブラウザで表示できるJPEGの閲覧用画像が作られる。"""
        result: ImportResult = photos.import_photo(conn, settings.photos_dir, image_bytes((320, 240), 'HEIF'), 'IMG_0002.HEIC', item_id=item_id)
        photo: Photo = photos.get_photo(conn, result.photo_id)
        assert photo.original_path.endswith('.heic')
        with Image.open(settings.photos_dir / photo.display_path) as display:
            assert (display.format, display.size) == ('JPEG', (320, 240))

    def test_transparent_png_is_converted_for_jpeg(self, conn: sqlite3.Connection, settings: Settings, item_id: int) -> None:
        """前提: 透過PNG / 操作: 取り込む / 期待: 閲覧用画像が作られる。"""
        image: Image.Image = Image.new('RGBA', (100, 100), (0, 0, 0, 0))
        path: Path = settings.data_dir / 'transparent.png'
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path)
        result: ImportResult = photos.import_photo(conn, settings.photos_dir, path.read_bytes(), 'transparent.png', item_id=item_id)
        assert (settings.photos_dir / photos.get_photo(conn, result.photo_id).display_path).exists()

    def test_same_content_is_detected_and_file_is_shared(self, conn: sqlite3.Connection, settings: Settings, places: Places, item_id: int) -> None:
        """前提: 書籍に取り込み済みの写真 / 操作: 同じ写真を箱に取り込む / 期待: 既存の紐付け先が示され、画像ファイルは共有される。"""
        content: bytes = image_bytes()
        first: ImportResult = photos.import_photo(conn, settings.photos_dir, content, 'a.jpg', item_id=item_id)
        second: ImportResult = photos.import_photo(conn, settings.photos_dir, content, 'b.jpg', container_id=places.box_a1)
        assert second.duplicates == ['物品「リーダブルコード」']
        assert photos.get_photo(conn, first.photo_id).original_path == photos.get_photo(conn, second.photo_id).original_path
        assert len(list((settings.photos_dir / 'originals').rglob('*.jpg'))) == 1

    def test_non_image_is_rejected(self, conn: sqlite3.Connection, settings: Settings, item_id: int) -> None:
        """前提: 画像でないファイル / 操作: 取り込む / 期待: 拒否され、写真は紐付かない。"""
        with pytest.raises(ValidationError):
            photos.import_photo(conn, settings.photos_dir, b'not an image', 'memo.txt', item_id=item_id)
        assert photos.list_photos(conn, item_id=item_id) == []

    def test_import_records_photo_link_in_history(self, conn: sqlite3.Connection, settings: Settings, item_id: int) -> None:
        """前提: 書籍 / 操作: 写真を取り込む / 期待: 修正の履歴に写真との対応が残る。"""
        photos.import_photo(conn, settings.photos_dir, image_bytes(), 'a.jpg', '表紙', item_id=item_id)
        latest: events.Event = events.list_events(conn, 'item', item_id)[0]
        assert (latest.kind, latest.memo) == ('update', '写真の紐付け')
        assert [photo['purpose'] for photo in (latest.after or {})['photos']] == ['表紙']


class TestLinks:
    def test_detach_keeps_file_and_history(self, conn: sqlite3.Connection, settings: Settings, item_id: int) -> None:
        """前提: 書籍に紐付いた写真 / 操作: 紐付けを解除する / 期待: 一覧から消えるが、画像ファイルは残り、履歴から辿れる。"""
        result: ImportResult = photos.import_photo(conn, settings.photos_dir, image_bytes(), 'a.jpg', item_id=item_id)
        photos.detach_photo(conn, result.photo_id)
        photo: Photo = photos.get_photo(conn, result.photo_id)
        assert photos.list_photos(conn, item_id=item_id) == []
        assert photos.list_photos(conn, item_id=item_id, include_detached=True) == [photo]
        assert (settings.photos_dir / photo.original_path).exists()
        assert events.list_events(conn, 'item', item_id)[0].memo == '写真の紐付け解除'

    def test_purpose_can_be_changed(self, conn: sqlite3.Connection, settings: Settings, item_id: int) -> None:
        """前提: 用途「表紙」の写真 / 操作: 用途を「裏表紙」に変える / 期待: 用途が変わり、履歴に残る。"""
        result: ImportResult = photos.import_photo(conn, settings.photos_dir, image_bytes(), 'a.jpg', '表紙', item_id=item_id)
        photos.set_purpose(conn, result.photo_id, '裏表紙')
        assert photos.get_photo(conn, result.photo_id).purpose == '裏表紙'
        assert events.list_events(conn, 'item', item_id)[0].memo == '写真の用途変更'

    def test_container_photo_is_recorded_in_container_history(self, conn: sqlite3.Connection, settings: Settings, places: Places) -> None:
        """前提: 箱A-01 / 操作: 箱の中身全体の写真を取り込む / 期待: 保管場所の履歴に残る。"""
        photos.import_photo(conn, settings.photos_dir, image_bytes(), 'box.jpg', '箱の中身全体', container_id=places.box_a1)
        assert len(photos.list_photos(conn, container_id=places.box_a1)) == 1
        assert events.list_events(conn, 'container', places.box_a1)[0].memo == '写真の紐付け'

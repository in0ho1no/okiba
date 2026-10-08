"""手動バックアップ・バックアップからの復元・更新の一時停止のテスト。"""

import json
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from okiba import __version__, events, items, photos, status
from okiba.backup import BackupError, BackupSummary, WriteGate, create_backup, verify_backup
from okiba.common import ValidationError
from okiba.config import Settings
from okiba.db import connect, open_database, schema_version
from tests.helpers import Places, book_input, image_bytes

CREATED: datetime = datetime.fromisoformat('2026-10-08T09:30:15+09:00')


@pytest.fixture
def stocked(conn: sqlite3.Connection, settings: Settings, places: Places) -> dict[str, int]:
    """写真付きの書籍、紐付けを解除した写真、削除済みの書籍。"""
    book: int = items.register_item(conn, book_input(places.box_a1))
    kept: photos.ImportResult = photos.import_photo(conn, settings.photos_dir, image_bytes(), 'front.jpg', '表紙', item_id=book)
    detached: photos.ImportResult = photos.import_photo(conn, settings.photos_dir, image_bytes(color=(0, 90, 200)), 'back.png', item_id=book)
    photos.detach_photo(conn, detached.photo_id)
    deleted: int = items.register_item(conn, book_input(places.box_a2, title='誤登録'))
    status.delete_item(conn, deleted, '二重登録')
    return {'book': book, 'kept_photo': kept.photo_id, 'detached_photo': detached.photo_id, 'deleted': deleted}


def _backup(settings: Settings, destination: Path) -> BackupSummary:
    destination.mkdir(exist_ok=True)
    return create_backup(settings.db_path, settings.photos_dir, str(destination), now=CREATED)


class TestCreateBackup:
    def test_zip_contains_db_all_originals_and_manifest(self, settings: Settings, stocked: dict[str, int], tmp_path: Path) -> None:
        """前提: 写真付き・紐付け解除・削除済みの登録 / 操作: バックアップする / 期待: 日付付きzipにDB・全元画像・閲覧用画像・マニフェストが入る。"""
        summary: BackupSummary = _backup(settings, tmp_path / 'out')
        assert summary.path == tmp_path / 'out' / 'okiba-backup-20261008-093015.zip'
        with zipfile.ZipFile(summary.path) as archive:
            names: set[str] = set(archive.namelist())
            manifest: dict[str, object] = json.loads(archive.read('manifest.json'))
        assert 'okiba.sqlite3' in names
        assert sum(1 for name in names if name.startswith('photos/originals/')) == 2
        assert sum(1 for name in names if name.startswith('photos/display/')) == 2
        assert manifest['app_version'] == __version__
        assert manifest['created_at'] == '2026-10-08T09:30:15+09:00'
        assert (summary.item_count, summary.photo_count, summary.file_count) == (2, 2, 6)

    def test_no_partial_file_is_left_after_success(self, settings: Settings, stocked: dict[str, int], tmp_path: Path) -> None:
        """前提: 登録済みのデータ / 操作: バックアップする / 期待: 出力先には完成したzipだけが残る。"""
        _backup(settings, tmp_path / 'out')
        assert [entry.name for entry in (tmp_path / 'out').iterdir()] == ['okiba-backup-20261008-093015.zip']

    def test_missing_original_fails_without_leaving_zip(self, settings: Settings, stocked: dict[str, int], tmp_path: Path) -> None:
        """前提: 画像フォルダから元画像が1つ失われている / 操作: バックアップする / 期待: 失敗として扱い、書きかけのzipも残さない。"""
        original: Path = next((settings.photos_dir / 'originals').rglob('*.jpg'))
        original.unlink()
        with pytest.raises(BackupError, match='元画像 1 件'):
            _backup(settings, tmp_path / 'out')
        assert list((tmp_path / 'out').iterdir()) == []

    @pytest.mark.parametrize('destination', ['', 'relative/folder', '{tmp}/missing'], ids=['空', '相対パス', '存在しない'])
    def test_invalid_destination_is_rejected(self, settings: Settings, conn: sqlite3.Connection, tmp_path: Path, destination: str) -> None:
        """前提: なし / 操作: 空・相対パス・存在しないフォルダを出力先にする / 期待: 出力先の入力エラーになる。"""
        with pytest.raises(ValidationError) as error:
            create_backup(settings.db_path, settings.photos_dir, destination.format(tmp=tmp_path))
        assert set(error.value.errors) == {'destination'}

    @pytest.mark.parametrize('subfolder', ['', 'originals', 'originals/ab'], ids=['写真フォルダ', '元画像フォルダ', 'その配下'])
    def test_destination_inside_photos_folder_is_rejected(self, settings: Settings, conn: sqlite3.Connection, subfolder: str) -> None:
        """前提: なし / 操作: 写真フォルダとその配下を出力先にする / 期待: 前回のzipが次に入らないよう、出力先の入力エラーになる。"""
        destination: Path = settings.photos_dir / subfolder
        destination.mkdir(parents=True, exist_ok=True)
        with pytest.raises(ValidationError) as error:
            create_backup(settings.db_path, settings.photos_dir, str(destination))
        assert '写真フォルダの中' in error.value.errors['destination']

    def test_unreadable_original_is_reported_but_backup_completes(self, settings: Settings, stocked: dict[str, int], tmp_path: Path) -> None:
        """前提: 画像フォルダの元画像1つが壊れている / 操作: バックアップする / 期待: zipは完成し、読み込めない元画像として示される。"""
        original: Path = next((settings.photos_dir / 'originals').rglob('*.jpg'))
        original.write_bytes(original.read_bytes()[:200])
        summary: BackupSummary = _backup(settings, tmp_path / 'out')
        assert summary.path.is_file()
        assert summary.unreadable == (original.relative_to(settings.photos_dir).as_posix(),)

    def test_readable_originals_are_not_reported(self, settings: Settings, stocked: dict[str, int], tmp_path: Path) -> None:
        """前提: 元画像がすべて正常 / 操作: バックアップする / 期待: 読み込めない元画像はない。"""
        assert _backup(settings, tmp_path / 'out').unreadable == ()

    def test_same_second_backup_does_not_overwrite(self, settings: Settings, conn: sqlite3.Connection, tmp_path: Path) -> None:
        """前提: 同じ日時のバックアップが既にある / 操作: もう一度バックアップする / 期待: 上書きせずに拒否される。"""
        _backup(settings, tmp_path / 'out')
        with pytest.raises(ValidationError):
            _backup(settings, tmp_path / 'out')


class TestVerifyAndRestore:
    def test_restored_backup_can_be_opened(self, settings: Settings, stocked: dict[str, int], tmp_path: Path) -> None:
        """前提: バックアップのzip / 操作: 展開して新しい保存先にする / 期待: 物品・履歴・写真（解除した写真を含む）を参照できる。"""
        summary: BackupSummary = _backup(settings, tmp_path / 'out')
        restored: Settings = Settings(data_dir=tmp_path / 'restored')
        with zipfile.ZipFile(summary.path) as archive:
            archive.extractall(restored.data_dir)
        open_database(restored.db_path)
        conn: sqlite3.Connection = connect(restored.db_path)
        try:
            assert items.get_item(conn, stocked['book']).name == 'リーダブルコード'
            assert items.get_item(conn, stocked['deleted']).deleted
            assert events.list_events(conn, 'item', stocked['deleted'])[0].memo == '二重登録'
            for photo in photos.list_photos(conn, item_id=stocked['book'], include_detached=True):
                assert (restored.photos_dir / photo.original_path).is_file()
                assert (restored.photos_dir / photo.thumb_path).is_file()
        finally:
            conn.close()

    def test_display_images_are_regenerated_from_originals(self, settings: Settings, stocked: dict[str, int], tmp_path: Path) -> None:
        """前提: 閲覧用画像・縮小版を除いて展開したバックアップ / 操作: 元画像から作り直す / 期待: すべての写真の閲覧用画像と縮小版がそろう。"""
        summary: BackupSummary = _backup(settings, tmp_path / 'out')
        restored: Settings = Settings(data_dir=tmp_path / 'restored')
        with zipfile.ZipFile(summary.path) as archive:
            for name in archive.namelist():
                if not name.startswith(('photos/display/', 'photos/thumbs/')):
                    archive.extract(name, restored.data_dir)
        conn: sqlite3.Connection = connect(restored.db_path)
        try:
            assert photos.regenerate_missing_images(conn, restored.photos_dir) == 2
            for photo in photos.list_photos(conn, item_id=stocked['book'], include_detached=True):
                assert (restored.photos_dir / photo.display_path).is_file()
                assert (restored.photos_dir / photo.thumb_path).is_file()
        finally:
            conn.close()

    def test_verify_reports_schema_version(self, settings: Settings, conn: sqlite3.Connection, tmp_path: Path) -> None:
        """前提: バックアップのzip / 操作: 検証する / 期待: アプリと同じDB形式のバージョンが分かる。"""
        summary: BackupSummary = _backup(settings, tmp_path / 'out')
        assert verify_backup(summary.path).schema_version == schema_version(conn)

    def test_verify_rejects_other_files(self, tmp_path: Path) -> None:
        """前提: zipでないファイルと、マニフェストのないzip / 操作: 検証する / 期待: どちらも失敗する。"""
        not_zip: Path = tmp_path / 'not.zip'
        not_zip.write_text('not a zip', encoding='utf-8')
        with pytest.raises(BackupError):
            verify_backup(not_zip)
        empty: Path = tmp_path / 'empty.zip'
        with zipfile.ZipFile(empty, 'w') as archive:
            archive.writestr('readme.txt', '')
        with pytest.raises(BackupError):
            verify_backup(empty)


class TestWriteGate:
    def test_writes_are_refused_during_backup(self) -> None:
        """前提: バックアップ中 / 操作: 更新を始める / 期待: 断られ、バックアップの終了後は始められる。"""
        gate: WriteGate = WriteGate()
        with gate.backup():
            assert gate.backing_up
            assert not gate.try_begin_write()
        assert gate.try_begin_write()

    def test_backup_waits_for_running_writes(self) -> None:
        """前提: 処理中の更新がある / 操作: バックアップを始める / 期待: 待ち時間内に終わらなければ始めず、更新は再び受け付ける。"""
        gate: WriteGate = WriteGate()
        assert gate.try_begin_write()
        with pytest.raises(BackupError), gate.backup(timeout=0.01):
            pass
        assert not gate.backing_up
        gate.end_write()
        with gate.backup(timeout=0.01):
            assert gate.backing_up

    def test_concurrent_backup_is_refused(self) -> None:
        """前提: バックアップ中 / 操作: もう1つバックアップを始める / 期待: 断られる。"""
        gate: WriteGate = WriteGate()
        with gate.backup(), pytest.raises(BackupError), gate.backup():
            pass

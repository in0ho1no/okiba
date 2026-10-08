"""手動バックアップ。DBと画像フォルダを、検証を済ませた日付付きのzipに書き出す。

zipの構成は、manifest.json（アプリ・DB形式のバージョンと作成日時）、okiba.sqlite3、photos/（画像フォルダの中身）とする。
復元は、ツールを停止してzipを展開し、データの保存先のDBと画像フォルダを置き換えて行う（手順は docs/development.md）。
"""

import json
import sqlite3
import tempfile
import threading
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from okiba import __version__
from okiba.common import ValidationError
from okiba.db import connect, schema_version
from okiba.photos import is_readable_image

BACKUP_FORMAT: str = 'okiba-backup'
BACKUP_FORMAT_VERSION: int = 1
MANIFEST_NAME: str = 'manifest.json'
DB_NAME: str = 'okiba.sqlite3'
PHOTOS_PREFIX: str = 'photos/'
PHOTO_SUBDIRS: tuple[str, ...] = ('originals', 'display', 'thumbs')
WRITER_WAIT_SECONDS: float = 30.0


class BackupError(Exception):
    """バックアップを完成できなかった。"""


@dataclass(frozen=True)
class BackupSummary:
    """検証済みのバックアップの内容。

    unreadable は画像として読み込めない元画像、unviewable はそのうち閲覧用画像か縮小版も使えず、復元後に表示できない写真の元画像。
    """

    path: Path
    created_at: str
    app_version: str
    schema_version: int
    item_count: int
    event_count: int
    photo_count: int
    file_count: int
    unreadable: tuple[str, ...] = ()
    unviewable: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """復元後にすべての写真を表示できるか。表示できない写真があれば不完全なバックアップとする。"""
        return not self.unviewable

    @property
    def damaged(self) -> tuple[str, ...]:
        """元画像は読み込めないが、閲覧用画像と縮小版があるため復元後も表示できる写真の元画像。"""
        return tuple(path for path in self.unreadable if path not in self.unviewable)


class WriteGate:
    """バックアップ中は更新を止める。

    更新要求は try_begin_write と end_write で囲む。バックアップは新しい更新を止め、処理中の更新が終わるのを待ってから始める。
    """

    def __init__(self) -> None:
        """更新もバックアップもない状態で作る。"""
        self._condition: threading.Condition = threading.Condition()
        self._writers: int = 0
        self._backing_up: bool = False

    def try_begin_write(self) -> bool:
        """更新を始めてよければ True を返す。バックアップ中なら False。"""
        with self._condition:
            if self._backing_up:
                return False
            self._writers += 1
            return True

    def end_write(self) -> None:
        """更新の終了を知らせる。"""
        with self._condition:
            self._writers -= 1
            self._condition.notify_all()

    @property
    def backing_up(self) -> bool:
        """バックアップ中かどうか。"""
        with self._condition:
            return self._backing_up

    @contextmanager
    def backup(self, timeout: float = WRITER_WAIT_SECONDS) -> Iterator[None]:
        """ブロックの間、更新を止める。"""
        with self._condition:
            if self._backing_up:
                raise BackupError('別のバックアップを実行中です。')
            self._backing_up = True
            if not self._condition.wait_for(lambda: self._writers == 0, timeout):
                self._backing_up = False
                raise BackupError('処理中の更新が終わらないため、バックアップを開始できませんでした。')
        try:
            yield
        finally:
            with self._condition:
                self._backing_up = False
                self._condition.notify_all()


def _copy_database(db_path: Path, destination: Path) -> None:
    """SQLiteのバックアップ機能で、更新途中の状態を含まないDBの複製を作る。"""
    source: sqlite3.Connection = connect(db_path)
    target: sqlite3.Connection = sqlite3.connect(destination)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def _photo_files(photos_dir: Path) -> list[str]:
    """画像フォルダ内の元画像・閲覧用画像・縮小版を、写真フォルダからの相対パス（区切りは /）で返す。"""
    files: list[str] = []
    for subdir in PHOTO_SUBDIRS:
        root: Path = photos_dir / subdir
        if not root.is_dir():
            continue
        for entry in sorted(root.rglob('*')):
            # 生成途中の一時ファイル（.tmp）は完成した画像ではないため含めない
            if entry.is_file() and entry.suffix != '.tmp':
                files.append(entry.relative_to(photos_dir).as_posix())
    return files


def _manifest(db_copy: Path, created_at: str) -> dict[str, Any]:
    conn: sqlite3.Connection = sqlite3.connect(db_copy)
    try:
        version: int = schema_version(conn)
    finally:
        conn.close()
    return {
        'format': BACKUP_FORMAT,
        'format_version': BACKUP_FORMAT_VERSION,
        'app_version': __version__,
        'schema_version': version,
        'created_at': created_at,
    }


def _write_zip(path: Path, db_copy: Path, photos_dir: Path, photo_files: list[str], manifest: dict[str, Any]) -> None:
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.write(db_copy, DB_NAME)
        for relative in photo_files:
            # 画像は圧縮済みの形式が多く、再圧縮しても小さくならないため無圧縮で格納する
            archive.write(photos_dir / relative, PHOTOS_PREFIX + relative, compress_type=zipfile.ZIP_STORED)


def _check_database(archive: zipfile.ZipFile, manifest: dict[str, Any], names: set[str]) -> tuple[int, int, list[tuple[str, str, str]]]:
    """zip内のDBを展開して開き、整合性と、写真の元画像がそろっていることを確かめる。

    （物品の件数, 履歴の件数, 写真ごとの（元画像, 閲覧用画像, 縮小版）のパス）を返す。
    """
    with tempfile.TemporaryDirectory(prefix='okiba-verify-') as work:
        extracted: Path = Path(archive.extract(DB_NAME, work))
        conn: sqlite3.Connection = sqlite3.connect(extracted)
        try:
            integrity: str = conn.execute('PRAGMA integrity_check').fetchone()[0]
            if integrity != 'ok':
                raise BackupError(f'DBの整合性の確認に失敗しました（{integrity}）。')
            if schema_version(conn) != manifest.get('schema_version'):
                raise BackupError('DB形式のバージョンがマニフェストと一致しません。')
            item_count: int = conn.execute('SELECT count(*) FROM items').fetchone()[0]
            event_count: int = conn.execute('SELECT count(*) FROM item_events').fetchone()[0]
            photo_paths: list[tuple[str, str, str]] = [
                (row[0], row[1], row[2]) for row in conn.execute('SELECT original_path, display_path, thumb_path FROM photos')
            ]
        finally:
            conn.close()
    missing: list[str] = [original for original, _, _ in photo_paths if PHOTOS_PREFIX + original not in names]
    if missing:
        raise BackupError(f'写真の元画像 {len(missing)} 件が画像フォルダにありません（例: {missing[0]}）。')
    return item_count, event_count, photo_paths


def _check_images(archive: zipfile.ZipFile, names: set[str], photo_paths: list[tuple[str, str, str]]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """（画像として読み込めない元画像, そのうち閲覧用画像か縮小版も使えず復元後に表示できない元画像）を返す。

    元画像が読み込めれば、閲覧用画像と縮小版は起動時に作り直せるため、zipになくても表示できる。
    """
    readable: dict[str, bool] = {}

    def is_readable(relative: str) -> bool:
        if relative not in readable:
            name: str = PHOTOS_PREFIX + relative
            readable[relative] = name in names and is_readable_image(archive.read(name))
        return readable[relative]

    unreadable: set[str] = set()
    unviewable: set[str] = set()
    for original, display, thumb in photo_paths:
        if is_readable(original):
            continue
        unreadable.add(original)
        if not (is_readable(display) and is_readable(thumb)):
            unviewable.add(original)
    return tuple(sorted(unreadable)), tuple(sorted(unviewable))


def verify_backup(path: Path) -> BackupSummary:
    """バックアップのzipを検証する。展開したDBから物品・履歴・写真を参照できなければ BackupError。

    画像として読み込めない元画像は unreadable、復元後に表示できない写真は unviewable に挙げるが、例外にはしない。
    元の画像フォルダで既に壊れている画像があると、直すまでバックアップを一切取れなくなるため。表示できない写真がある場合は complete が偽になる。
    """
    try:
        with zipfile.ZipFile(path) as archive:
            broken: str | None = archive.testzip()
            if broken is not None:
                raise BackupError(f'zipの中身が壊れています（{broken}）。')
            names: set[str] = set(archive.namelist())
            if MANIFEST_NAME not in names or DB_NAME not in names:
                raise BackupError('マニフェストまたはDBが含まれていません。')
            manifest: dict[str, Any] = json.loads(archive.read(MANIFEST_NAME))
            if manifest.get('format') != BACKUP_FORMAT:
                raise BackupError('Okibaのバックアップではありません。')
            item_count: int
            event_count: int
            photo_paths: list[tuple[str, str, str]]
            item_count, event_count, photo_paths = _check_database(archive, manifest, names)
            unreadable: tuple[str, ...]
            unviewable: tuple[str, ...]
            unreadable, unviewable = _check_images(archive, names, photo_paths)
    except zipfile.BadZipFile as error:
        raise BackupError('zipとして読み込めません。') from error
    return BackupSummary(
        path=path,
        created_at=manifest['created_at'],
        app_version=manifest['app_version'],
        schema_version=manifest['schema_version'],
        item_count=item_count,
        event_count=event_count,
        photo_count=len(photo_paths),
        file_count=sum(1 for name in names if name.startswith(PHOTOS_PREFIX)),
        unreadable=unreadable,
        unviewable=unviewable,
    )


def create_backup(db_path: Path, photos_dir: Path, destination: str, now: datetime | None = None) -> BackupSummary:
    """DBと画像フォルダを日付付きのzipに書き出し、検証済みのバックアップの内容を返す。

    書き出し中は「.partial」付きの名前にし、検証に通った場合だけ正式な名前に変える。失敗した場合は書きかけのzipを残さない。
    復元後に表示できない写真がある場合は、完成したバックアップと取り違えないよう「-incomplete」付きの名前で残す。
    """
    raw: str = destination.strip()
    folder: Path = Path(raw).expanduser()
    if not raw or not folder.is_absolute():
        raise ValidationError({'destination': '出力先フォルダを絶対パスで入力してください。'})
    if not folder.is_dir():
        raise ValidationError({'destination': '出力先フォルダが見つかりません。'})
    if folder.resolve().is_relative_to(photos_dir.resolve()):
        raise ValidationError(
            {'destination': '写真フォルダの中は出力先にできません。前回のバックアップが次のバックアップに画像として入ってしまうためです。'}
        )
    created: datetime = now or datetime.now().astimezone()
    stem: str = f'okiba-backup-{created:%Y%m%d-%H%M%S}'
    final: Path = folder / f'{stem}.zip'
    incomplete: Path = folder / f'{stem}-incomplete.zip'
    if final.exists() or incomplete.exists():
        raise ValidationError({'destination': '同じ名前のバックアップが既にあります。少し待ってから実行してください。'})
    partial: Path = final.with_name(final.name + '.partial')
    try:
        with tempfile.TemporaryDirectory(prefix='okiba-backup-') as work:
            db_copy: Path = Path(work) / DB_NAME
            _copy_database(db_path, db_copy)
            manifest: dict[str, Any] = _manifest(db_copy, created.isoformat(timespec='seconds'))
            _write_zip(partial, db_copy, photos_dir, _photo_files(photos_dir), manifest)
        summary: BackupSummary = verify_backup(partial)
        if not summary.complete:
            final = incomplete
        partial.replace(final)
    except OSError as error:
        partial.unlink(missing_ok=True)
        raise BackupError(f'バックアップを書き出せませんでした（{error}）。') from error
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return replace(summary, path=final)

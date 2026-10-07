"""SQLite接続・トランザクション・マイグレーション。"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from importlib.resources.abc import Traversable
from itertools import count
from pathlib import Path

_savepoint_ids: Iterator[int] = count(1)


class DatabaseVersionError(Exception):
    """DB形式がアプリより新しく、安全に開けない。"""


def connect(path: Path | str) -> sqlite3.Connection:
    """外部キー制約を有効にした接続を返す。

    トランザクションは transaction() で明示的に管理するため、自動トランザクションは使わない。
    リクエストごとに接続を作り、非同期ハンドラーとスレッドプールをまたいで使うため check_same_thread を無効にする。
    """
    conn: sqlite3.Connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """ブロック内の更新をすべて確定するか、すべて取り消す。入れ子の場合はセーブポイントを使う。"""
    if conn.in_transaction:
        name: str = f'sp_{next(_savepoint_ids)}'
        conn.execute(f'SAVEPOINT {name}')
        try:
            yield conn
        except BaseException:
            conn.execute(f'ROLLBACK TO {name}')
            conn.execute(f'RELEASE {name}')
            raise
        conn.execute(f'RELEASE {name}')
        return
    conn.execute('BEGIN IMMEDIATE')
    try:
        yield conn
    except BaseException:
        conn.execute('ROLLBACK')
        raise
    conn.execute('COMMIT')


def load_migrations() -> list[str]:
    """番号順に並べたマイグレーションSQLを返す。リストの位置+1がDB形式のバージョンになる。"""
    package: Traversable = resources.files('okiba') / 'migrations'
    files: list[Traversable] = sorted(
        (entry for entry in package.iterdir() if entry.name.endswith('.sql')),
        key=lambda entry: entry.name,
    )
    return [entry.read_text(encoding='utf-8') for entry in files]


def schema_version(conn: sqlite3.Connection) -> int:
    """DB形式のバージョン（PRAGMA user_version）を返す。"""
    row: sqlite3.Row = conn.execute('PRAGMA user_version').fetchone()
    return int(row[0])


def migrate(conn: sqlite3.Connection) -> int:
    """未適用のマイグレーションを順に適用し、適用後のバージョンを返す。"""
    migrations: list[str] = load_migrations()
    current: int = schema_version(conn)
    latest: int = len(migrations)
    if current > latest:
        raise DatabaseVersionError(f'DB形式のバージョン {current} はこのアプリ（対応バージョン {latest}）より新しいため開けません。')
    for version in range(current + 1, latest + 1):
        script: str = migrations[version - 1]
        try:
            conn.executescript(f'BEGIN;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;')
        except BaseException:
            if conn.in_transaction:
                conn.execute('ROLLBACK')
            raise
    return latest


def open_database(db_path: Path) -> None:
    """DBを作成または更新する。古い形式の場合は、更新前のDBを同じフォルダへ退避してから更新する。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn: sqlite3.Connection = connect(db_path)
    try:
        current: int = schema_version(conn)
        latest: int = len(load_migrations())
        if 0 < current < latest:
            backup_path: Path = db_path.with_name(f'{db_path.stem}.v{current}.bak{db_path.suffix}')
            backup_conn: sqlite3.Connection = sqlite3.connect(backup_path)
            try:
                conn.backup(backup_conn)
            finally:
                backup_conn.close()
        migrate(conn)
    finally:
        conn.close()

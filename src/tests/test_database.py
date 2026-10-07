"""DBの作成・DB形式のバージョン・トランザクション・スキーマ上の制約のテスト。"""

import sqlite3
from pathlib import Path

import pytest

from okiba import db
from okiba.db import DatabaseVersionError, connect, load_migrations, open_database, schema_version, transaction
from tests.helpers import CABLES_ID, Places


class TestDatabaseVersion:
    def test_new_database_has_latest_version_and_initial_data(self, conn: sqlite3.Connection) -> None:
        """前提: DBがない / 操作: 起動時の準備を行う / 期待: 最新形式で作られ、初期テンプレートと保管場所の種類が入っている。"""
        assert schema_version(conn) == len(load_migrations())
        names: list[str] = [row['name'] for row in conn.execute('SELECT name FROM categories ORDER BY id')]
        assert names == ['書籍', 'ケーブル類']
        kinds: list[str] = [row['name'] for row in conn.execute('SELECT name FROM container_kinds ORDER BY sort_order')]
        assert kinds == ['部屋', '家具', '段', '箱']

    def test_reopening_does_not_apply_migrations_twice(self, tmp_path: Path) -> None:
        """前提: 準備済みのDB / 操作: もう一度準備を行う / 期待: 初期データが重複しない。"""
        path: Path = tmp_path / 'okiba.sqlite3'
        open_database(path)
        open_database(path)
        conn: sqlite3.Connection = connect(path)
        count: int = conn.execute('SELECT count(*) FROM categories').fetchone()[0]
        conn.close()
        assert count == 2

    def test_newer_database_version_is_rejected(self, tmp_path: Path) -> None:
        """前提: アプリより新しい形式のDB / 操作: 準備を行う / 期待: 開かずにエラーにする。"""
        path: Path = tmp_path / 'okiba.sqlite3'
        open_database(path)
        conn: sqlite3.Connection = connect(path)
        conn.execute('PRAGMA user_version = 999')
        conn.close()
        with pytest.raises(DatabaseVersionError):
            open_database(path)

    def test_older_database_is_backed_up_before_migration(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """前提: 1つ前の形式のDB / 操作: 新しいマイグレーションを含むアプリで準備する / 期待: 更新前のDBを退避してから更新する。"""
        path: Path = tmp_path / 'okiba.sqlite3'
        open_database(path)
        current: list[str] = load_migrations()
        monkeypatch.setattr(db, 'load_migrations', lambda: [*current, 'CREATE TABLE added_later (id INTEGER PRIMARY KEY);'])
        open_database(path)
        backup: Path = tmp_path / f'okiba.v{len(current)}.bak.sqlite3'
        assert backup.exists()
        conn: sqlite3.Connection = connect(path)
        assert schema_version(conn) == len(current) + 1
        conn.close()
        backup_conn: sqlite3.Connection = connect(backup)
        assert schema_version(backup_conn) == len(current)
        backup_conn.close()


def _insert_tag_then_fail(conn: sqlite3.Connection, name: str) -> None:
    with transaction(conn):
        conn.execute("INSERT INTO tags (name, created_at, updated_at) VALUES (?, 'x', 'x')", (name,))
        raise RuntimeError


class TestTransaction:
    def test_error_rolls_back_all_changes(self, conn: sqlite3.Connection) -> None:
        """前提: なし / 操作: 更新の途中で例外が起きる / 期待: 途中までの更新も取り消される。"""
        with pytest.raises(RuntimeError):
            _insert_tag_then_fail(conn, '一時')
        assert conn.execute('SELECT count(*) FROM tags').fetchone()[0] == 0

    def test_nested_error_rolls_back_only_inner_block(self, conn: sqlite3.Connection) -> None:
        """前提: 外側のトランザクション内 / 操作: 内側のブロックで例外を捕捉する / 期待: 内側の更新だけが取り消される。"""
        with transaction(conn):
            conn.execute("INSERT INTO tags (name, created_at, updated_at) VALUES ('外側', 'x', 'x')")
            with pytest.raises(RuntimeError):
                _insert_tag_then_fail(conn, '内側')
        names: list[str] = [row['name'] for row in conn.execute('SELECT name FROM tags')]
        assert names == ['外側']


class TestSchemaConstraints:
    def test_stored_item_requires_container(self, conn: sqlite3.Connection) -> None:
        """前提: なし / 操作: 保管場所のない保管中の物品を直接書き込む / 期待: DBの制約で拒否される。"""
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                'INSERT INTO items (name, category_id, quantity, status, container_id, created_at, updated_at) '
                "VALUES ('x', ?, 1, 'stored', NULL, 'x', 'x')",
                (CABLES_ID,),
            )

    def test_released_item_must_not_keep_container(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: なし / 操作: 保管場所を持ったままの売却済の物品を直接書き込む / 期待: DBの制約で拒否される。"""
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                'INSERT INTO items (name, category_id, quantity, status, container_id, created_at, updated_at) '
                "VALUES ('x', ?, 1, 'sold', ?, 'x', 'x')",
                (CABLES_ID, places.box_a1),
            )

    def test_quantity_must_be_positive(self, conn: sqlite3.Connection, places: Places) -> None:
        """前提: なし / 操作: 数量0の物品を直接書き込む / 期待: DBの制約で拒否される。"""
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO items (name, category_id, quantity, container_id, created_at, updated_at) VALUES ('x', ?, 0, ?, 'x', 'x')",
                (CABLES_ID, places.box_a1),
            )

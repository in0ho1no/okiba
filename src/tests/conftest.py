"""テスト共通のフィクスチャ。

テストは仕様（docs/spec.md）の振る舞い単位で書き、docstring に「前提・操作・期待」を記す。
各テストは一時フォルダに新しいDBと写真フォルダを作るため、利用者の実データには触れない。
"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from okiba.config import Settings
from okiba.db import connect, open_database
from okiba.web.app import create_app
from tests.helpers import Places, make_places


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """一時フォルダをデータの保存先にした設定。"""
    return Settings(data_dir=tmp_path / 'data')


@pytest.fixture
def conn(settings: Settings) -> Iterator[sqlite3.Connection]:
    """初期データ投入済みのDB接続。"""
    open_database(settings.db_path)
    connection: sqlite3.Connection = connect(settings.db_path)
    yield connection
    connection.close()


@pytest.fixture
def places(conn: sqlite3.Connection) -> Places:
    """書斎 › 本棚 › 上段 › 箱A-01 / 箱A-02 と、押入れ › 箱B-01 の保管場所。"""
    return make_places(conn)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """画面のHTTPテスト用クライアント。最初に画面を開き、CSRF対策のトークンをCookieで受け取っておく。"""
    with TestClient(create_app(settings)) as test_client:
        test_client.get('/')
        yield test_client

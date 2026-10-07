"""起動処理のテスト。"""

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

import main
from okiba.config import DATA_DIR_ENV, Settings, load_settings


def test_data_dir_defaults_outside_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    """前提: 保存先の環境変数なし / 操作: 設定を読み込む / 期待: ホームディレクトリ配下の okiba-data を使う。"""
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)
    assert load_settings().data_dir == Path.home() / 'okiba-data'


def test_main_listens_only_on_localhost(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """前提: 保存先を一時フォルダに指定 / 操作: ポート8123で起動する / 期待: 127.0.0.1だけで待ち受け、保存先にDBが作られる。"""
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path / 'data'))
    calls: list[dict[str, Any]] = []

    def fake_run(app: FastAPI, **options: Any) -> None:
        calls.append({'app': app, **options})

    monkeypatch.setattr(main.uvicorn, 'run', fake_run)
    main.main(['--port', '8123'])
    assert len(calls) == 1
    assert (calls[0]['host'], calls[0]['port']) == ('127.0.0.1', 8123)
    settings: Settings = calls[0]['app'].state.settings
    assert settings.db_path.exists()

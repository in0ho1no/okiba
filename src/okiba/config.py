"""実行時設定。"""

import os
from dataclasses import dataclass
from pathlib import Path

DATA_DIR_ENV: str = 'OKIBA_DATA_DIR'


@dataclass(frozen=True)
class Settings:
    """データの保存先と待ち受け設定。"""

    data_dir: Path
    host: str = '127.0.0.1'
    port: int = 8000

    @property
    def db_path(self) -> Path:
        """SQLiteデータベースのパス。"""
        return self.data_dir / 'okiba.sqlite3'

    @property
    def photos_dir(self) -> Path:
        """取り込んだ写真の保存先。"""
        return self.data_dir / 'photos'


def load_settings() -> Settings:
    """環境変数から設定を読み込む。

    公開リポジトリに私物のデータが混入しないよう、既定の保存先はリポジトリ外のホームディレクトリ配下にする。
    """
    raw: str | None = os.environ.get(DATA_DIR_ENV)
    data_dir: Path = Path(raw) if raw else Path.home() / 'okiba-data'
    return Settings(data_dir=data_dir.expanduser())

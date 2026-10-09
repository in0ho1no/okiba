"""Okiba を起動する。

`uv run python src/main.py` で起動し、ブラウザで表示されたURLを開く。
データの保存先は環境変数 OKIBA_DATA_DIR で変更できる（既定はホームディレクトリの okiba-data）。
"""

import argparse
import sys

import uvicorn
from fastapi import FastAPI

from okiba.config import Settings, load_settings
from okiba.db import DatabaseVersionError
from okiba.web.app import create_app


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """コマンドライン引数を解釈する。"""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description='私物管理ツール Okiba を起動します。')
    parser.add_argument('--port', type=int, default=8000, help='待ち受けるポート番号（既定：8000）')
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """ローカルホストだけで待ち受けてアプリを起動する。DB形式がアプリより新しい場合は、理由を表示して起動しない。"""
    args: argparse.Namespace = parse_args(argv)
    settings: Settings = load_settings()
    print(f'データの保存先: {settings.data_dir}')
    try:
        app: FastAPI = create_app(settings)
    except DatabaseVersionError as error:
        print(f'起動できません: {error}', file=sys.stderr)
        print('新しいバージョンのOkibaで開くか、このバージョンで作成したバックアップから復元してください。', file=sys.stderr)
        raise SystemExit(1) from error
    print(f'ブラウザで http://{settings.host}:{args.port}/ を開いてください。')
    uvicorn.run(app, host=settings.host, port=args.port)


if __name__ == '__main__':
    main()

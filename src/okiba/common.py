"""モジュール横断で使う例外・正規化・日時の補助関数。"""

import unicodedata
from datetime import datetime


class ValidationError(Exception):
    """入力内容の検証エラー。キーは入力欄名、値は表示するメッセージ。"""

    def __init__(self, errors: dict[str, str]) -> None:
        """エラー内容を保持する。"""
        super().__init__('; '.join(f'{key}: {message}' for key, message in errors.items()))
        self.errors: dict[str, str] = errors


class NotFoundError(Exception):
    """対象のデータが存在しない。"""


def now_iso() -> str:
    """現在時刻をタイムゾーン付きISO 8601文字列で返す。"""
    return datetime.now().astimezone().isoformat(timespec='seconds')


def clean_text(value: str) -> str:
    """入力文字列の前後の空白を除く。"""
    return value.strip()


def search_normalize(value: str) -> str:
    """検索・比較用に、全角半角と大文字小文字の違いを吸収する。"""
    return unicodedata.normalize('NFKC', value).casefold()


def format_number(value: float) -> str:
    """整数値の小数点以下を省いて表示用文字列にする。"""
    if float(value).is_integer():
        return str(int(value))
    return f'{value:g}'

"""CSRF（クロスサイトリクエストフォージェリ）対策。

認証のないローカルアプリでも、起動中にブラウザで開いた別サイトから 127.0.0.1 へフォームを送られうる。
Cookieに置いたトークンと、フォームに埋め込んだ同じトークンが一致しない更新要求は拒否する。
別サイトはCookieの値を読めず、SameSite=Strict のCookieは別サイトからの送信に付かないため、一致させられない。
"""

import re
import secrets
from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import Response

COOKIE_NAME: str = 'okiba_csrf'
FIELD_NAME: str = 'csrf_token'
SAFE_METHODS: frozenset[str] = frozenset({'GET', 'HEAD', 'OPTIONS'})

_TOKEN_PATTERN: re.Pattern[str] = re.compile(r'^[A-Za-z0-9_-]{43}$')


class CsrfError(Exception):
    """CSRF対策の検証に失敗した。"""


def _valid_cookie(request: Request) -> str | None:
    token: str | None = request.cookies.get(COOKIE_NAME)
    return token if token is not None and _TOKEN_PATTERN.fullmatch(token) else None


async def issue_token(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """リクエストごとにトークンを用意し、Cookieになければ発行する（HTTPミドルウェア）。"""
    token: str | None = _valid_cookie(request)
    issued: bool = token is None
    if token is None:
        token = secrets.token_urlsafe(32)
    request.state.csrf_token = token
    response: Response = await call_next(request)
    if issued:
        response.set_cookie(COOKIE_NAME, token, httponly=True, samesite='strict', path='/')
    return response


async def verify_token(request: Request) -> None:
    """更新要求（GET以外）のトークンと送信元を検証する（ルーターの依存関係）。"""
    if request.method in SAFE_METHODS:
        return
    origin: str | None = request.headers.get('origin')
    if origin is not None and origin != f'{request.url.scheme}://{request.url.netloc}':
        raise CsrfError('別のサイトからの送信です。')
    cookie: str | None = _valid_cookie(request)
    submitted: object = (await request.form()).get(FIELD_NAME)
    if cookie is None or not isinstance(submitted, str) or not secrets.compare_digest(submitted, cookie):
        raise CsrfError('フォームの確認用トークンが一致しません。')

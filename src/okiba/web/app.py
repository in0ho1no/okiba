"""アプリケーションの組み立て。"""

import sqlite3
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from okiba import __version__, photos
from okiba.backup import WriteGate
from okiba.common import NotFoundError
from okiba.config import Settings, load_settings
from okiba.db import connect, open_database
from okiba.web import backup_views, item_views, manage_views, status_views
from okiba.web.common import STATIC_DIR, render
from okiba.web.csrf import SAFE_METHODS, CsrfError, issue_token


async def pause_writes_during_backup(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """バックアップ中は更新要求を503で断り、閲覧だけを許可する（HTTPミドルウェア）。"""
    if request.method in SAFE_METHODS or request.url.path == backup_views.BACKUP_PATH:
        return await call_next(request)
    gate: WriteGate = request.app.state.write_gate
    if not gate.try_begin_write():
        return render(request, 'busy.html', {}, status_code=503)
    try:
        return await call_next(request)
    finally:
        gate.end_write()


def create_app(settings: Settings | None = None) -> FastAPI:
    """設定に従ってDBを準備し、画面を組み込んだアプリを返す。"""
    resolved: Settings = settings or load_settings()
    open_database(resolved.db_path)
    resolved.photos_dir.mkdir(parents=True, exist_ok=True)
    conn: sqlite3.Connection = connect(resolved.db_path)
    try:
        photos.regenerate_missing_images(conn, resolved.photos_dir)
    finally:
        conn.close()
    app: FastAPI = FastAPI(title='Okiba', version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = resolved
    app.state.write_gate = WriteGate()
    app.middleware('http')(issue_token)
    app.middleware('http')(pause_writes_during_backup)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost'])
    app.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')
    app.mount('/media', StaticFiles(directory=resolved.photos_dir), name='media')
    app.include_router(item_views.router)
    app.include_router(status_views.router)
    app.include_router(manage_views.router)
    app.include_router(backup_views.router)

    @app.exception_handler(NotFoundError)
    async def not_found(request: Request, error: NotFoundError) -> HTMLResponse:
        return render(request, 'not_found.html', {}, status_code=404)

    @app.exception_handler(CsrfError)
    async def csrf_failed(request: Request, error: CsrfError) -> HTMLResponse:
        return render(request, 'forbidden.html', {'reason': str(error)}, status_code=403)

    return app

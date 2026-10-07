"""アプリケーションの組み立て。"""

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from okiba import __version__
from okiba.common import NotFoundError
from okiba.config import Settings, load_settings
from okiba.db import open_database
from okiba.web import item_views, manage_views
from okiba.web.common import STATIC_DIR, render
from okiba.web.csrf import CsrfError, issue_token


def create_app(settings: Settings | None = None) -> FastAPI:
    """設定に従ってDBを準備し、画面を組み込んだアプリを返す。"""
    resolved: Settings = settings or load_settings()
    open_database(resolved.db_path)
    resolved.photos_dir.mkdir(parents=True, exist_ok=True)
    app: FastAPI = FastAPI(title='Okiba', version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = resolved
    app.middleware('http')(issue_token)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost'])
    app.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')
    app.mount('/media', StaticFiles(directory=resolved.photos_dir), name='media')
    app.include_router(item_views.router)
    app.include_router(manage_views.router)

    @app.exception_handler(NotFoundError)
    async def not_found(request: Request, error: NotFoundError) -> HTMLResponse:
        return render(request, 'not_found.html', {}, status_code=404)

    @app.exception_handler(CsrfError)
    async def csrf_failed(request: Request, error: CsrfError) -> HTMLResponse:
        return render(request, 'forbidden.html', {'reason': str(error)}, status_code=403)

    return app

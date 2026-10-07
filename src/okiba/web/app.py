"""アプリケーションの組み立て。"""

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from okiba import __version__
from okiba.common import NotFoundError
from okiba.config import Settings, load_settings
from okiba.db import open_database
from okiba.web import item_views, manage_views
from okiba.web.common import STATIC_DIR, render


def create_app(settings: Settings | None = None) -> FastAPI:
    """設定に従ってDBを準備し、画面を組み込んだアプリを返す。"""
    resolved: Settings = settings or load_settings()
    open_database(resolved.db_path)
    resolved.photos_dir.mkdir(parents=True, exist_ok=True)
    app: FastAPI = FastAPI(title='Okiba', version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = resolved
    app.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')
    app.mount('/media', StaticFiles(directory=resolved.photos_dir), name='media')
    app.include_router(item_views.router)
    app.include_router(manage_views.router)

    @app.exception_handler(NotFoundError)
    async def not_found(request: Request, error: NotFoundError) -> HTMLResponse:
        return render(request, 'not_found.html', {}, status_code=404)

    return app

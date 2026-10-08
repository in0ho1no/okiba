"""手動バックアップの画面。"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData

from okiba.backup import BackupError, BackupSummary, WriteGate, create_backup
from okiba.common import ValidationError
from okiba.config import Settings
from okiba.web.common import render, text_value
from okiba.web.csrf import verify_token

router: APIRouter = APIRouter(dependencies=[Depends(verify_token)])

# この画面の送信は、バックアップ中の更新停止の対象から外す（バックアップ自身が更新の終了を待つため）。
BACKUP_PATH: str = '/backup'


@router.get(BACKUP_PATH, response_class=HTMLResponse)
def backup_page(request: Request) -> HTMLResponse:
    """バックアップの画面。"""
    return render(request, 'backup.html', {'nav': 'manage', 'destination': '', 'summary': None})


@router.post(BACKUP_PATH, response_class=HTMLResponse)
async def backup_submit(request: Request) -> HTMLResponse:
    """バックアップの実行。書き出しの間は更新を止め、閲覧の要求は受け付け続ける。"""
    data: FormData = await request.form()
    destination: str = text_value(data.get('destination'))
    settings: Settings = request.app.state.settings
    gate: WriteGate = request.app.state.write_gate

    def run() -> BackupSummary:
        with gate.backup():
            return create_backup(settings.db_path, settings.photos_dir, destination)

    context: dict[str, object] = {'nav': 'manage', 'destination': destination, 'summary': None}
    try:
        # 書き出しは時間がかかるため、閲覧の要求を止めないようスレッドで実行する
        summary: BackupSummary = await run_in_threadpool(run)
    except ValidationError as error:
        return render(request, 'backup.html', {**context, 'errors': error.errors}, status_code=422)
    except BackupError as error:
        return render(request, 'backup.html', {**context, 'errors': {'backup': str(error)}}, status_code=500)
    return render(request, 'backup.html', {**context, 'summary': summary})

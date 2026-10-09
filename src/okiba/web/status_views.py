"""状態の変更・誤登録削除と復元・履歴からの差し戻しの画面。"""

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from starlette.datastructures import FormData

from okiba import items, revert, status
from okiba.common import NotFoundError, ValidationError
from okiba.revert import RevertPlan
from okiba.status import StatusRecord
from okiba.web.common import Conn, optional_int, redirect, render, text_value
from okiba.web.csrf import verify_token
from okiba.web.item_views import detail_context

router: APIRouter = APIRouter(dependencies=[Depends(verify_token)])

# 操作名と、完了後に表示するお知らせの対応。
_STATUS_NOTICES: dict[str, str] = {
    'take_out': 'taken_out',
    'put_back': 'put_back',
    'lend': 'lent',
    'give_back': 'given_back',
    'sell': 'sold',
    'dispose': 'disposed',
    'undo_release': 'release_undone',
}

_FORM_KEYS: tuple[str, ...] = ('date', 'party', 'note', 'reason', 'container_id')


def _record(data: FormData) -> StatusRecord:
    return StatusRecord(date=text_value(data.get('date')), party=text_value(data.get('party')), note=text_value(data.get('note')))


def _retry(request: Request, conn: sqlite3.Connection, item_id: int, operation: str, data: FormData, error: ValidationError) -> HTMLResponse:
    """入力内容を保ったまま詳細画面を再表示する。入力欄のエラーは「操作名.欄名」のキーで渡す。"""
    errors: dict[str, str] = {key if key == 'item' else f'{operation}.{key}': message for key, message in error.errors.items()}
    status_form: dict[str, str] = {'op': operation, **{key: text_value(data.get(key)) for key in _FORM_KEYS}}
    return render(request, 'item_detail.html', {**detail_context(conn, item_id, status_form), 'errors': errors}, status_code=422)


def _apply_status(conn: sqlite3.Connection, item_id: int, operation: str, data: FormData) -> None:
    container_id: int | None = optional_int(data.get('container_id'))
    if operation == 'take_out':
        status.take_out(conn, item_id)
    elif operation == 'put_back':
        status.put_back(conn, item_id, container_id)
    elif operation == 'lend':
        status.lend(conn, item_id, _record(data))
    elif operation == 'give_back':
        status.give_back(conn, item_id, container_id)
    elif operation == 'sell':
        status.release(conn, item_id, 'sold', _record(data))
    elif operation == 'dispose':
        status.release(conn, item_id, 'disposed', _record(data))
    elif operation == 'undo_release':
        status.undo_release(conn, item_id, container_id)
    else:
        raise NotFoundError(f'status operation {operation}')


@router.post('/items/{item_id}/status/{operation}', response_class=HTMLResponse)
async def status_submit(request: Request, conn: Conn, item_id: int, operation: str) -> Response:
    """取り出す・戻す・貸出・返却・売却・廃棄・手放しの取消の送信。"""
    data: FormData = await request.form()
    try:
        _apply_status(conn, item_id, operation, data)
    except ValidationError as error:
        return _retry(request, conn, item_id, operation, data, error)
    return redirect(f'/items/{item_id}?notice={_STATUS_NOTICES[operation]}')


@router.post('/items/{item_id}/delete', response_class=HTMLResponse)
async def delete_submit(request: Request, conn: Conn, item_id: int) -> Response:
    """誤登録削除の送信。削除理由がなければ削除しない。"""
    data: FormData = await request.form()
    try:
        status.delete_item(conn, item_id, text_value(data.get('reason')))
    except ValidationError as error:
        return _retry(request, conn, item_id, 'delete', data, error)
    return redirect(f'/items/{item_id}?notice=deleted')


@router.post('/items/{item_id}/restore', response_class=HTMLResponse)
async def restore_submit(request: Request, conn: Conn, item_id: int) -> Response:
    """誤登録削除からの復元の送信。"""
    data: FormData = await request.form()
    try:
        status.restore_item(conn, item_id, optional_int(data.get('container_id')))
    except ValidationError as error:
        return _retry(request, conn, item_id, 'restore', data, error)
    return redirect(f'/items/{item_id}?notice=restored')


def _revert_context(conn: sqlite3.Connection, item_id: int, event_id: int) -> dict[str, Any]:
    plan: RevertPlan = revert.plan_revert(conn, item_id, event_id)
    return {'nav': 'search', 'item': items.get_item(conn, item_id), 'plan': plan}


@router.get('/items/{item_id}/revert/{event_id}', response_class=HTMLResponse)
def revert_page(request: Request, conn: Conn, item_id: int, event_id: int) -> HTMLResponse:
    """履歴からの差し戻しの確認画面。現在との差分を表示する。"""
    try:
        context: dict[str, Any] = _revert_context(conn, item_id, event_id)
    except ValidationError as error:
        return render(request, 'item_detail.html', {**detail_context(conn, item_id), 'errors': error.errors}, status_code=422)
    return render(request, 'revert.html', context)


@router.post('/items/{item_id}/revert/{event_id}', response_class=HTMLResponse)
def revert_submit(request: Request, conn: Conn, item_id: int, event_id: int) -> Response:
    """履歴からの差し戻しの確定。"""
    try:
        context: dict[str, Any] = _revert_context(conn, item_id, event_id)
    except ValidationError as error:
        return render(request, 'item_detail.html', {**detail_context(conn, item_id), 'errors': error.errors}, status_code=422)
    try:
        changed: bool = revert.apply_revert(conn, item_id, event_id)
    except ValidationError as error:
        return render(request, 'revert.html', {**context, 'errors': error.errors}, status_code=422)
    return redirect(f'/items/{item_id}?notice={"reverted" if changed else "unchanged"}')

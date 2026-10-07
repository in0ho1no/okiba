"""登録・検索・詳細・内容の修正・移動・写真の画面。"""

import sqlite3
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from starlette.datastructures import FormData, QueryParams, UploadFile

from okiba import catalog, containers, events, items, photos, tags
from okiba.catalog import Field
from okiba.common import ValidationError
from okiba.config import Settings
from okiba.db import transaction
from okiba.forms import (
    ADHOC_PREFIX,
    ItemForm,
    copy_form,
    form_from_item,
    form_from_mapping,
    form_to_input,
    move_unmatched_to_extras,
)
from okiba.items import ACTIVE_STATUSES, STATUS_LABELS, ItemDetail, ItemInput, SearchQuery
from okiba.naming import is_cable_template
from okiba.web.common import Conn, category_selection, optional_int, redirect, render, text_value
from okiba.web.csrf import verify_token

router: APIRouter = APIRouter(dependencies=[Depends(verify_token)])


@router.get('/', response_class=HTMLResponse)
def search_page(request: Request, conn: Conn) -> HTMLResponse:
    """検索・更新の画面。状態の既定の絞り込みは所持中。"""
    params: QueryParams = request.query_params
    searched: bool = params.get('searched') == '1'
    statuses: tuple[str, ...] = tuple(status for status in params.getlist('status') if status in STATUS_LABELS) if searched else ACTIVE_STATUSES
    query: SearchQuery = SearchQuery(
        text=params.get('q', ''),
        category_id=optional_int(params.get('category_id')),
        container_id=optional_int(params.get('container_id')),
        tag_id=optional_int(params.get('tag_id')),
        statuses=statuses,
    )
    return render(
        request,
        'search.html',
        {
            'nav': 'search',
            'query': query,
            'results': items.search_items(conn, query),
            'categories': catalog.list_categories(conn),
            'container_paths': containers.all_paths(conn),
            'tags': tags.list_tags(conn),
        },
    )


def _fields(conn: sqlite3.Connection, form: ItemForm) -> list[Field]:
    return catalog.fields_for_category(conn, form.category_id) if form.category_id is not None else []


def _extra_labels(conn: sqlite3.Connection, form: ItemForm) -> dict[str, str]:
    """テンプレート外の項目の表示名。廃止済みの項目は定義の表示名、テンプレートに保存しない項目は入力した名前。"""
    labels: dict[str, str] = {}
    known: dict[str, str] = {}
    if form.category_id is not None:
        known = {item_field.key: item_field.label for item_field in catalog.fields_for_category(conn, form.category_id, include_retired=True)}
    for key in form.extras:
        labels[key] = key.removeprefix(ADHOC_PREFIX) if key.startswith(ADHOC_PREFIX) else known.get(key, key)
    return labels


def _form_context(conn: sqlite3.Connection, form: ItemForm, errors: dict[str, str], mode: str, item: ItemDetail | None = None) -> dict[str, Any]:
    fields: list[Field] = _fields(conn, form)
    return {
        'nav': 'register' if mode == 'new' else 'search',
        'mode': mode,
        'item': item,
        'form': form,
        'errors': errors,
        'majors': catalog.top_categories(conn),
        'minors': catalog.child_categories(conn, form.major_id) if form.major_id is not None else [],
        'fields': fields,
        'suggestions': {item_field.key: catalog.suggestions(conn, item_field.key) for item_field in fields if item_field.field_type == 'suggest'},
        'extra_labels': _extra_labels(conn, form),
        'name_label': catalog.name_label(conn, form.category_id) if form.category_id is not None else '名称',
        'is_cable': is_cable_template(item_field.key for item_field in fields),
        'container_paths': containers.all_paths(conn),
        'all_tags': tags.list_tags(conn),
    }


def _normalize_selection(conn: sqlite3.Connection, form: ItemForm) -> None:
    """大カテゴリを変えたときに、別の大カテゴリの小カテゴリが選ばれたままにならないようにする。"""
    if form.major_id is None:
        form.minor_id = None
        return
    if form.minor_id is not None and form.minor_id not in {child.id for child in catalog.child_categories(conn, form.major_id)}:
        form.minor_id = None


def _apply_form_action(conn: sqlite3.Connection, form: ItemForm, data: FormData, action: str) -> dict[str, str]:
    """保存以外の操作（カテゴリ作成・項目追加）を反映し、エラーがあれば返す。"""
    try:
        if action == 'add_major':
            form.major_id = catalog.create_category(conn, text_value(data.get('new_major_name')))
            form.minor_id = None
        elif action == 'add_minor':
            if form.major_id is None:
                return {'new_minor_name': '先に大カテゴリを選んでください。'}
            form.minor_id = catalog.create_category(conn, text_value(data.get('new_minor_name')), form.major_id)
        elif action == 'add_field':
            return _add_field_from_form(conn, form, data)
    except ValidationError as error:
        prefix: str = 'new_major_' if action == 'add_major' else 'new_minor_' if action == 'add_minor' else 'new_field_'
        return {f'{prefix}{key}': message for key, message in error.errors.items()}
    return {}


def _add_field_from_form(conn: sqlite3.Connection, form: ItemForm, data: FormData) -> dict[str, str]:
    if form.category_id is None:
        return {'new_field_label': '先にカテゴリを選んでください。'}
    label: str = text_value(data.get('new_field_label')).strip()
    if not label:
        return {'new_field_label': '表示名を入力してください。'}
    if data.get('new_field_save') == 'on':
        unit_kind: str | None = text_value(data.get('new_field_unit_kind')) or None
        catalog.add_field(conn, form.category_id, label, text_value(data.get('new_field_type')) or 'text', unit_kind=unit_kind)
    else:
        form.extras.setdefault(f'{ADHOC_PREFIX}{label}', '')
    return {}


def _uploads(data: FormData) -> list[UploadFile]:
    return [upload for upload in data.getlist('photos') if isinstance(upload, UploadFile) and upload.filename]


@router.get('/items/new', response_class=HTMLResponse)
def new_item_page(request: Request, conn: Conn) -> HTMLResponse:
    """登録画面。copy_from を指定すると、その物品の内容を引き継いだ複製登録の画面になる。"""
    params: QueryParams = request.query_params
    copy_from: int | None = optional_int(params.get('copy_from'))
    form: ItemForm
    if copy_from is not None:
        source: ItemDetail = items.get_item(conn, copy_from)
        major_id: int
        minor_id: int | None
        major_id, minor_id = category_selection(conn, source.category_id)
        form = copy_form(source, major_id, minor_id, catalog.fields_for_category(conn, source.category_id))
    else:
        form = ItemForm(major_id=optional_int(params.get('major_id')), minor_id=optional_int(params.get('minor_id')))
        _normalize_selection(conn, form)
    return render(request, 'item_form.html', _form_context(conn, form, {}, 'new'))


@router.post('/items/new', response_class=HTMLResponse)
async def new_item_submit(request: Request, conn: Conn) -> Response:
    """登録画面の送信。保存以外の操作では入力内容を保ったまま画面を再表示する。"""
    data: FormData = await request.form()
    form: ItemForm = form_from_mapping(data)
    _normalize_selection(conn, form)
    action: str = text_value(data.get('action')) or 'refresh'
    if action not in ('save', 'save_continue'):
        errors: dict[str, str] = _apply_form_action(conn, form, data, action)
        return render(request, 'item_form.html', _form_context(conn, form, errors, 'new'), status_code=422 if errors else 200)
    fields: list[Field] = _fields(conn, form)
    item_input: ItemInput
    parse_errors: dict[str, str]
    item_input, parse_errors = form_to_input(form, fields)
    uploads: list[UploadFile] = _uploads(data)
    purpose: str = text_value(data.get('photo_purpose'))
    settings: Settings = request.app.state.settings
    duplicates: list[str] = []
    if parse_errors:
        errors = {**items.validate_item(conn, item_input), **parse_errors}
        return render(request, 'item_form.html', _form_context(conn, form, errors, 'new'), status_code=422)
    try:
        with transaction(conn):
            item_id: int = items.register_item(conn, item_input)
            for upload in uploads:
                result: photos.ImportResult = photos.import_photo(
                    conn, settings.photos_dir, await upload.read(), upload.filename or '', purpose, item_id=item_id
                )
                duplicates.extend(result.duplicates)
    except ValidationError as error:
        return render(request, 'item_form.html', _form_context(conn, form, error.errors, 'new'), status_code=422)
    query: dict[str, str] = {'notice': 'registered'}
    if duplicates:
        query['dup'] = '、'.join(duplicates)
    if action == 'save_continue':
        return redirect(f'/items/new?{urlencode({"copy_from": item_id, **query})}')
    return redirect(f'/items/{item_id}?{urlencode(query)}')


def _detail_attributes(conn: sqlite3.Connection, item: ItemDetail) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """詳細画面に表示する（テンプレートの項目, テンプレート外の項目）を、それぞれ（表示名, 値）の並びで返す。"""
    current: list[Field] = catalog.fields_for_category(conn, item.category_id)
    all_fields: dict[str, Field] = {
        item_field.key: item_field for item_field in catalog.fields_for_category(conn, item.category_id, include_retired=True)
    }
    template_values: list[tuple[str, str]] = []
    for item_field in current:
        if item_field.identifier_kind:
            continue
        value: Any = item.attributes.get(item_field.key)
        template_values.append((item_field.label, items.display_value(value)))
    current_keys: set[str] = {item_field.key for item_field in current}
    extras: list[tuple[str, str]] = []
    for key, value in item.attributes.items():
        if key in current_keys:
            continue
        label: str = key.removeprefix(ADHOC_PREFIX) if key.startswith(ADHOC_PREFIX) else all_fields[key].label if key in all_fields else key
        extras.append((label, items.display_value(value)))
    return template_values, extras


def _detail_context(conn: sqlite3.Connection, item_id: int) -> dict[str, Any]:
    item: ItemDetail = items.get_item(conn, item_id)
    template_values: list[tuple[str, str]]
    extras: list[tuple[str, str]]
    template_values, extras = _detail_attributes(conn, item)
    return {
        'nav': 'search',
        'item': item,
        'template_values': template_values,
        'extras': extras,
        'photos': photos.list_photos(conn, item_id=item_id),
        'events': events.list_events(conn, 'item', item_id),
        'container_paths': containers.all_paths(conn),
    }


@router.get('/items/{item_id}', response_class=HTMLResponse)
def item_detail(request: Request, conn: Conn, item_id: int) -> HTMLResponse:
    """詳細画面。"""
    return render(request, 'item_detail.html', _detail_context(conn, item_id))


@router.get('/items/{item_id}/edit', response_class=HTMLResponse)
def edit_item_page(request: Request, conn: Conn, item_id: int) -> HTMLResponse:
    """内容の修正画面。"""
    item: ItemDetail = items.get_item(conn, item_id)
    major_id: int
    minor_id: int | None
    major_id, minor_id = category_selection(conn, item.category_id)
    form: ItemForm = form_from_item(item, major_id, minor_id, catalog.fields_for_category(conn, item.category_id))
    return render(request, 'item_form.html', _form_context(conn, form, {}, 'edit', item))


@router.post('/items/{item_id}/edit', response_class=HTMLResponse)
async def edit_item_submit(request: Request, conn: Conn, item_id: int) -> Response:
    """内容の修正の送信。カテゴリを変えた場合、移行先にない属性はテンプレート外の項目として残す。"""
    item: ItemDetail = items.get_item(conn, item_id)
    data: FormData = await request.form()
    form: ItemForm = form_from_mapping(data)
    _normalize_selection(conn, form)
    action: str = text_value(data.get('action')) or 'refresh'
    previous_category_id: int | None = optional_int(data.get('prev_category_id'))
    if action not in ('save',):
        errors: dict[str, str] = _apply_form_action(conn, form, data, action)
        if previous_category_id is not None and form.category_id is not None and previous_category_id != form.category_id:
            move_unmatched_to_extras(
                form, catalog.fields_for_category(conn, previous_category_id), catalog.fields_for_category(conn, form.category_id)
            )
        return render(request, 'item_form.html', _form_context(conn, form, errors, 'edit', item), status_code=422 if errors else 200)
    fields: list[Field] = _fields(conn, form)
    item_input: ItemInput
    parse_errors: dict[str, str]
    item_input, parse_errors = form_to_input(form, fields, item.attributes, item.identifiers)
    if parse_errors:
        errors = {**items.validate_item(conn, item_input, item_id), **parse_errors}
        return render(request, 'item_form.html', _form_context(conn, form, errors, 'edit', item), status_code=422)
    try:
        items.update_item(conn, item_id, item_input)
    except ValidationError as error:
        return render(request, 'item_form.html', _form_context(conn, form, error.errors, 'edit', item), status_code=422)
    return redirect(f'/items/{item_id}?notice=updated')


@router.post('/items/{item_id}/move', response_class=HTMLResponse)
async def move_item_submit(request: Request, conn: Conn, item_id: int) -> Response:
    """移動の送信。"""
    data: FormData = await request.form()
    container_id: int | None = optional_int(data.get('container_id'))
    try:
        if container_id is None:
            raise ValidationError({'container_id': '移動先を選んでください。'})
        items.move_item(conn, item_id, container_id)
    except ValidationError as error:
        return render(request, 'item_detail.html', {**_detail_context(conn, item_id), 'errors': error.errors}, status_code=422)
    return redirect(f'/items/{item_id}?notice=moved')


async def import_uploads(request: Request, conn: sqlite3.Connection, *, item_id: int | None = None, container_id: int | None = None) -> str:
    """アップロードされた写真を取り込み、移動先URLのクエリを返す。"""
    data: FormData = await request.form()
    uploads: list[UploadFile] = _uploads(data)
    if not uploads:
        raise ValidationError({'photo': '写真ファイルを選んでください。'})
    settings: Settings = request.app.state.settings
    purpose: str = text_value(data.get('photo_purpose'))
    duplicates: list[str] = []
    with transaction(conn):
        for upload in uploads:
            result: photos.ImportResult = photos.import_photo(
                conn, settings.photos_dir, await upload.read(), upload.filename or '', purpose, item_id=item_id, container_id=container_id
            )
            duplicates.extend(result.duplicates)
    query: dict[str, str] = {'notice': 'photo_added'}
    if duplicates:
        query['dup'] = '、'.join(duplicates)
    return urlencode(query)


@router.post('/items/{item_id}/photos', response_class=HTMLResponse)
async def item_photos_submit(request: Request, conn: Conn, item_id: int) -> Response:
    """物品への写真の追加。"""
    try:
        query: str = await import_uploads(request, conn, item_id=item_id)
    except ValidationError as error:
        return render(request, 'item_detail.html', {**_detail_context(conn, item_id), 'errors': error.errors}, status_code=422)
    return redirect(f'/items/{item_id}?{query}')


def _owner_url(photo: photos.Photo) -> str:
    return f'/items/{photo.item_id}' if photo.item_id is not None else f'/containers/{photo.container_id}'


@router.post('/photos/{photo_id}/detach', response_class=HTMLResponse)
def detach_photo_submit(conn: Conn, photo_id: int) -> Response:
    """写真の紐付けの解除。"""
    photo: photos.Photo = photos.get_photo(conn, photo_id)
    photos.detach_photo(conn, photo_id)
    return redirect(f'{_owner_url(photo)}?notice=photo_detached')


@router.post('/photos/{photo_id}/purpose', response_class=HTMLResponse)
async def photo_purpose_submit(request: Request, conn: Conn, photo_id: int) -> Response:
    """写真の用途の変更。"""
    data: FormData = await request.form()
    photo: photos.Photo = photos.get_photo(conn, photo_id)
    photos.set_purpose(conn, photo_id, text_value(data.get('purpose')))
    return redirect(f'{_owner_url(photo)}?notice=photo_purpose')

"""管理画面（カテゴリとテンプレート・タグ・保管場所）。"""

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from starlette.datastructures import FormData

from okiba import catalog, containers, events, items, photos, tags
from okiba.catalog import Category
from okiba.common import ValidationError
from okiba.web.common import Conn, optional_int, redirect, render, text_value
from okiba.web.csrf import verify_token
from okiba.web.item_views import import_uploads

router: APIRouter = APIRouter(dependencies=[Depends(verify_token)])


@router.get('/manage', response_class=HTMLResponse)
def manage_page(request: Request) -> HTMLResponse:
    """管理画面の入口。"""
    return render(request, 'manage.html', {'nav': 'manage'})


def _categories_context(conn: sqlite3.Connection) -> dict[str, Any]:
    majors: list[Category] = catalog.top_categories(conn)
    return {'nav': 'manage', 'majors': majors, 'children': {major.id: catalog.child_categories(conn, major.id) for major in majors}}


@router.get('/categories', response_class=HTMLResponse)
def categories_page(request: Request, conn: Conn) -> HTMLResponse:
    """カテゴリ一覧と作成フォーム。"""
    return render(request, 'categories.html', _categories_context(conn))


@router.post('/categories', response_class=HTMLResponse)
async def create_category_submit(request: Request, conn: Conn) -> Response:
    """カテゴリの作成。"""
    data: FormData = await request.form()
    try:
        category_id: int = catalog.create_category(conn, text_value(data.get('name')), optional_int(data.get('parent_id')))
    except ValidationError as error:
        return render(request, 'categories.html', {**_categories_context(conn), 'errors': error.errors, 'posted': data}, status_code=422)
    return redirect(f'/categories/{category_id}?notice=created')


def _category_context(conn: sqlite3.Connection, category_id: int) -> dict[str, Any]:
    category: Category = catalog.get_category(conn, category_id)
    inherited: list[catalog.Field] = catalog.own_fields(conn, category.parent_id) if category.parent_id is not None else []
    return {
        'nav': 'manage',
        'category': category,
        'path': catalog.category_path(conn, category_id),
        'fields': catalog.own_fields(conn, category_id),
        'inherited': inherited,
    }


@router.get('/categories/{category_id}', response_class=HTMLResponse)
def category_page(request: Request, conn: Conn, category_id: int) -> HTMLResponse:
    """カテゴリの名称変更とテンプレートの編集。"""
    return render(request, 'category_detail.html', _category_context(conn, category_id))


@router.post('/categories/{category_id}/rename', response_class=HTMLResponse)
async def rename_category_submit(request: Request, conn: Conn, category_id: int) -> Response:
    """カテゴリ名の変更。"""
    data: FormData = await request.form()
    try:
        catalog.rename_category(conn, category_id, text_value(data.get('name')))
    except ValidationError as error:
        return render(request, 'category_detail.html', {**_category_context(conn, category_id), 'errors': error.errors}, status_code=422)
    return redirect(f'/categories/{category_id}?notice=updated')


@router.post('/categories/{category_id}/fields', response_class=HTMLResponse)
async def add_field_submit(request: Request, conn: Conn, category_id: int) -> Response:
    """テンプレートへの項目の追加。"""
    data: FormData = await request.form()
    sort_order: int | None = optional_int(data.get('sort_order'))
    try:
        catalog.add_field(
            conn,
            category_id,
            text_value(data.get('label')),
            text_value(data.get('field_type')),
            unit_kind=text_value(data.get('unit_kind')) or None,
            required=data.get('required') == 'on',
            sort_order=sort_order,
            clear_on_copy=data.get('clear_on_copy') == 'on',
            dedupe=data.get('dedupe') == 'on',
            identifier_kind=text_value(data.get('identifier_kind')) or None,
            key=text_value(data.get('key')) or None,
        )
    except ValidationError as error:
        return render(
            request,
            'category_detail.html',
            {**_category_context(conn, category_id), 'errors': {f'new_{key}': message for key, message in error.errors.items()}, 'posted': data},
            status_code=422,
        )
    return redirect(f'/categories/{category_id}?notice=updated')


@router.post('/fields/{field_id}', response_class=HTMLResponse)
async def update_field_submit(request: Request, conn: Conn, field_id: int) -> Response:
    """項目の変更可能な設定の更新。"""
    data: FormData = await request.form()
    item_field: catalog.Field = catalog.get_field(conn, field_id)
    try:
        catalog.update_field(
            conn,
            field_id,
            label=text_value(data.get('label')),
            sort_order=optional_int(data.get('sort_order')) or 0,
            required=data.get('required') == 'on',
            clear_on_copy=data.get('clear_on_copy') == 'on',
            dedupe=data.get('dedupe') == 'on',
        )
    except ValidationError as error:
        return render(
            request,
            'category_detail.html',
            {
                **_category_context(conn, item_field.category_id),
                'errors': {f'field{field_id}_{key}': message for key, message in error.errors.items()},
            },
            status_code=422,
        )
    return redirect(f'/categories/{item_field.category_id}?notice=updated')


def _tags_context(conn: sqlite3.Connection) -> dict[str, Any]:
    return {'nav': 'manage', 'tags': tags.list_tags(conn)}


@router.get('/tags', response_class=HTMLResponse)
def tags_page(request: Request, conn: Conn) -> HTMLResponse:
    """タグ一覧。"""
    return render(request, 'tags.html', _tags_context(conn))


@router.post('/tags', response_class=HTMLResponse)
async def create_tag_submit(request: Request, conn: Conn) -> Response:
    """タグの作成。"""
    data: FormData = await request.form()
    try:
        tags.create_tag(conn, text_value(data.get('name')))
    except ValidationError as error:
        return render(request, 'tags.html', {**_tags_context(conn), 'errors': error.errors}, status_code=422)
    return redirect('/tags?notice=created')


@router.post('/tags/{tag_id}/rename', response_class=HTMLResponse)
async def rename_tag_submit(request: Request, conn: Conn, tag_id: int) -> Response:
    """タグ名の変更。"""
    data: FormData = await request.form()
    try:
        tags.rename_tag(conn, tag_id, text_value(data.get('name')))
    except ValidationError as error:
        return render(
            request, 'tags.html', {**_tags_context(conn), 'errors': {f'tag{tag_id}_{key}': m for key, m in error.errors.items()}}, status_code=422
        )
    return redirect('/tags?notice=updated')


def _containers_context(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        'nav': 'manage',
        'container_paths': containers.all_paths(conn),
        'kinds': containers.list_kinds(conn),
        'suggested_label': containers.suggest_label(conn),
    }


@router.get('/containers', response_class=HTMLResponse)
def containers_page(request: Request, conn: Conn) -> HTMLResponse:
    """保管場所の一覧と作成フォーム。"""
    return render(request, 'containers.html', _containers_context(conn))


@router.post('/containers', response_class=HTMLResponse)
async def create_container_submit(request: Request, conn: Conn) -> Response:
    """保管場所の作成。"""
    data: FormData = await request.form()
    try:
        container_id: int = containers.create_container(
            conn,
            text_value(data.get('name')),
            optional_int(data.get('kind_id')) or 0,
            optional_int(data.get('parent_id')),
            text_value(data.get('label')),
        )
    except ValidationError as error:
        return render(request, 'containers.html', {**_containers_context(conn), 'errors': error.errors, 'posted': data}, status_code=422)
    return redirect(f'/containers/{container_id}?notice=created')


@router.post('/container-kinds', response_class=HTMLResponse)
async def add_kind_submit(request: Request, conn: Conn) -> Response:
    """保管場所の種類の追加。"""
    data: FormData = await request.form()
    try:
        containers.add_kind(conn, text_value(data.get('kind_name')))
    except ValidationError as error:
        return render(request, 'containers.html', {**_containers_context(conn), 'errors': error.errors}, status_code=422)
    return redirect('/containers?notice=created')


def _container_context(conn: sqlite3.Connection, request: Request, container_id: int) -> dict[str, Any]:
    include_nested: bool = request.query_params.get('nested') == '1'
    container: containers.Container = containers.get_container(conn, container_id)
    excluded: set[int] = containers.descendant_ids(conn, container_id)
    return {
        'nav': 'manage',
        'container': container,
        'path': containers.container_path(conn, container_id),
        'include_nested': include_nested,
        'contents': items.container_contents(conn, container_id, include_nested=include_nested),
        'kinds': containers.list_kinds(conn),
        'parent_choices': [(other, path) for other, path in containers.all_paths(conn) if other.id not in excluded],
        'photos': photos.list_photos(conn, container_id=container_id),
        'events': events.list_events(conn, 'container', container_id),
    }


@router.get('/containers/{container_id}', response_class=HTMLResponse)
def container_page(request: Request, conn: Conn, container_id: int) -> HTMLResponse:
    """箱の中身と、保管場所の編集・移動・写真。"""
    return render(request, 'container_detail.html', _container_context(conn, request, container_id))


@router.post('/containers/{container_id}/edit', response_class=HTMLResponse)
async def edit_container_submit(request: Request, conn: Conn, container_id: int) -> Response:
    """保管場所の名前・種類・ラベルIDの変更。"""
    data: FormData = await request.form()
    try:
        containers.update_container(
            conn, container_id, text_value(data.get('name')), optional_int(data.get('kind_id')) or 0, text_value(data.get('label'))
        )
    except ValidationError as error:
        return render(request, 'container_detail.html', {**_container_context(conn, request, container_id), 'errors': error.errors}, status_code=422)
    return redirect(f'/containers/{container_id}?notice=updated')


@router.post('/containers/{container_id}/move', response_class=HTMLResponse)
async def move_container_submit(request: Request, conn: Conn, container_id: int) -> Response:
    """保管場所の親の変更。"""
    data: FormData = await request.form()
    try:
        containers.move_container(conn, container_id, optional_int(data.get('parent_id')))
    except ValidationError as error:
        return render(request, 'container_detail.html', {**_container_context(conn, request, container_id), 'errors': error.errors}, status_code=422)
    return redirect(f'/containers/{container_id}?notice=moved')


@router.post('/containers/{container_id}/photos', response_class=HTMLResponse)
async def container_photos_submit(request: Request, conn: Conn, container_id: int) -> Response:
    """保管場所への写真の追加。"""
    try:
        query: str = await import_uploads(request, conn, container_id=container_id)
    except ValidationError as error:
        return render(request, 'container_detail.html', {**_container_context(conn, request, container_id), 'errors': error.errors}, status_code=422)
    return redirect(f'/containers/{container_id}?{query}')

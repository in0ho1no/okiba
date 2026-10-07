-- フェーズ1全体（分割元・統合先・廃止状態・手放しの取消など）を見越したスキーマ。
-- 日時はタイムゾーン付きISO 8601文字列で保持する。

CREATE TABLE categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    parent_id INTEGER REFERENCES categories (id),
    name_label TEXT,
    retired INTEGER NOT NULL DEFAULT 0 CHECK (retired IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE UNIQUE INDEX categories_unique_name ON categories (ifnull(parent_id, 0), name);

CREATE TABLE category_fields (
    id INTEGER PRIMARY KEY,
    category_id INTEGER NOT NULL REFERENCES categories (id),
    key TEXT NOT NULL CHECK (key GLOB '[a-z]*' AND key NOT GLOB '*[^a-z0-9_]*'),
    label TEXT NOT NULL CHECK (length(trim(label)) > 0),
    field_type TEXT NOT NULL CHECK (field_type IN ('text', 'suggest', 'number', 'measure', 'date', 'bool')),
    unit_kind TEXT CHECK (unit_kind IN ('length')),
    required INTEGER NOT NULL DEFAULT 0 CHECK (required IN (0, 1)),
    sort_order INTEGER NOT NULL DEFAULT 0,
    clear_on_copy INTEGER NOT NULL DEFAULT 0 CHECK (clear_on_copy IN (0, 1)),
    dedupe INTEGER NOT NULL DEFAULT 0 CHECK (dedupe IN (0, 1)),
    identifier_kind TEXT CHECK (identifier_kind IN ('isbn', 'jan', 'model', 'serial')),
    retired INTEGER NOT NULL DEFAULT 0 CHECK (retired IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (category_id, key),
    CHECK ((field_type = 'measure') = (unit_kind IS NOT NULL)),
    CHECK (identifier_kind IS NULL OR field_type = 'text')
) STRICT;

CREATE TABLE container_kinds (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE CHECK (length(trim(name)) > 0),
    sort_order INTEGER NOT NULL DEFAULT 0
) STRICT;

CREATE TABLE containers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    kind_id INTEGER NOT NULL REFERENCES container_kinds (id),
    parent_id INTEGER REFERENCES containers (id),
    label TEXT UNIQUE,
    retired INTEGER NOT NULL DEFAULT 0 CHECK (retired IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE tags (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE CHECK (length(trim(name)) > 0),
    retired INTEGER NOT NULL DEFAULT 0 CHECK (retired IN (0, 1)),
    merged_into_id INTEGER REFERENCES tags (id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE item_events (
    id INTEGER PRIMARY KEY,
    target_type TEXT NOT NULL CHECK (target_type IN ('item', 'container')),
    target_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('create', 'update', 'move', 'status', 'split', 'merge', 'delete', 'restore')),
    before_json TEXT CHECK (before_json IS NULL OR json_valid(before_json)),
    after_json TEXT CHECK (after_json IS NULL OR json_valid(after_json)),
    memo TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
) STRICT;

CREATE INDEX item_events_target ON item_events (target_type, target_id, id);

CREATE TABLE items (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    category_id INTEGER NOT NULL REFERENCES categories (id),
    quantity INTEGER NOT NULL CHECK (quantity >= 1),
    status TEXT NOT NULL DEFAULT 'stored' CHECK (status IN ('stored', 'in_use', 'lent', 'sold', 'disposed')),
    -- 保管中は保管場所、使用中・貸出中は戻し先。手放した物は空にする。
    container_id INTEGER REFERENCES containers (id),
    note TEXT NOT NULL DEFAULT '',
    attributes TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(attributes) AND json_type(attributes) = 'object'),
    status_date TEXT,
    status_party TEXT,
    status_note TEXT,
    release_event_id INTEGER REFERENCES item_events (id),
    deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1)),
    deleted_at TEXT,
    split_from_id INTEGER REFERENCES items (id),
    merged_into_id INTEGER REFERENCES items (id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((status IN ('stored', 'in_use', 'lent')) = (container_id IS NOT NULL))
) STRICT;

CREATE INDEX items_container ON items (container_id);
CREATE INDEX items_category ON items (category_id);

CREATE TABLE identifiers (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items (id),
    kind TEXT NOT NULL CHECK (kind IN ('isbn', 'jan', 'model', 'serial')),
    value TEXT NOT NULL CHECK (length(value) > 0),
    UNIQUE (item_id, kind, value)
) STRICT;

CREATE INDEX identifiers_kind_value ON identifiers (kind, value);

CREATE TABLE item_tags (
    item_id INTEGER NOT NULL REFERENCES items (id),
    tag_id INTEGER NOT NULL REFERENCES tags (id),
    PRIMARY KEY (item_id, tag_id)
) STRICT;

CREATE TABLE photos (
    id INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL,
    original_path TEXT NOT NULL,
    display_path TEXT NOT NULL,
    thumb_path TEXT NOT NULL,
    original_name TEXT NOT NULL DEFAULT '',
    purpose TEXT NOT NULL DEFAULT '',
    item_id INTEGER REFERENCES items (id),
    container_id INTEGER REFERENCES containers (id),
    detached INTEGER NOT NULL DEFAULT 0 CHECK (detached IN (0, 1)),
    created_at TEXT NOT NULL,
    CHECK ((item_id IS NULL) <> (container_id IS NULL))
) STRICT;

CREATE INDEX photos_sha256 ON photos (sha256);

CREATE TABLE lookup_cache (
    code TEXT NOT NULL,
    provider TEXT NOT NULL,
    data TEXT NOT NULL CHECK (json_valid(data)),
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (code, provider)
) STRICT;

-- rowid を items.id と一致させ、正規化済みの検索用テキストをアプリ側で書き込む。
CREATE VIRTUAL TABLE item_search USING fts5 (body, tokenize = 'trigram');

INSERT INTO container_kinds (name, sort_order) VALUES ('部屋', 10), ('家具', 20), ('段', 30), ('箱', 40);

INSERT INTO categories (id, name, parent_id, name_label, created_at, updated_at)
VALUES
    (1, '書籍', NULL, 'タイトル', strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'), strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    (2, 'ケーブル類', NULL, NULL, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'), strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'));

INSERT INTO category_fields (
    category_id, key, label, field_type, unit_kind, required, sort_order, clear_on_copy, dedupe, identifier_kind, created_at, updated_at
)
VALUES
    (1, 'author', '著者', 'text', NULL, 0, 10, 0, 0, NULL, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'), strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    (1, 'publisher', '出版社', 'text', NULL, 0, 20, 0, 0, NULL, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'), strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    (1, 'isbn', 'ISBN', 'text', NULL, 0, 30, 0, 0, 'isbn', strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'), strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    (2, 'connector_a', '端子A', 'suggest', NULL, 0, 10, 0, 1, NULL, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'), strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    (2, 'connector_b', '端子B', 'suggest', NULL, 0, 20, 0, 1, NULL, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'), strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    (2, 'length', '長さ', 'measure', 'length', 0, 30, 0, 0, NULL, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'), strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'));

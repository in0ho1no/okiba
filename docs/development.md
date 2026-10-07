# 開発ガイド

仕様は [spec.md](spec.md) を正本とする。本書は起動方法と、デグレを防ぐためのテストの書き方をまとめる。

## 起動

```bash
uv run python src/main.py
```

- `127.0.0.1` だけで待ち受ける。ポートは `--port` で変更できる（既定は8000）。
- データ（SQLiteのDBと写真フォルダ）の保存先は、環境変数 `OKIBA_DATA_DIR` で指定する。未指定の場合はホームディレクトリの `okiba-data` を使う。
- 公開リポジトリに私物のデータが混入しないよう、保存先はリポジトリの外に置く。

## 構成

| パス | 内容 |
| --- | --- |
| `src/main.py` | 起動処理 |
| `src/okiba/migrations/` | DB形式の変更。`0001_initial.sql` から番号順に適用し、番号がDB形式のバージョンになる |
| `src/okiba/*.py` | 業務ロジック（カテゴリ、保管場所、物品、写真、検索、変更履歴） |
| `src/okiba/web/` | 画面（FastAPIのルーティング、Jinja2テンプレート、CSS・JavaScript） |
| `src/tests/` | テスト |

DB形式を変えるときは、既存のSQLファイルを書き換えず、次の番号のSQLファイルを追加する。起動時に、更新前のDBを `okiba.v<旧バージョン>.bak.sqlite3` として退避してから適用する。

## テスト

```bash
uv run pytest
```

テストは仕様の振る舞いごとに書き、docstringに「前提・操作・期待」を1行で記す。Gherkinの Given / When / Then に当たる。

```python
def test_cable_name_is_generated_from_attributes(self, conn, places) -> None:
    """前提: 名称を空にしたケーブル / 操作: 登録する / 期待: 端子と長さから名称が付く。"""
```

テストは次の2層で構成する。

| 層 | ファイル | 目的 |
| --- | --- | --- |
| 業務ロジック | `test_catalog.py` など | 仕様の規則（検証、名称の自動生成、履歴、検索）を細かく確認する |
| 画面のHTTPシナリオ | `test_web.py` | 依存パッケージ（FastAPI・Starlette・Jinja2・python-multipart・Pillow・pillow-heif）を更新しても、登録から検索までの操作が壊れていないことを確認する |

- 各テストは一時フォルダに新しいDBを作るため、実データには触れない。
- 仕様を変更・追加したときは、対応するテストを先に追加または修正する。
- 依存パッケージを更新したときは、`uv run pytest` がすべて通ることを確認する。

## CI

`.github/workflows/test.yml` が、`develop` へのpushとプルリクエストで次を実行する。

| ジョブ | 内容 |
| --- | --- |
| `lint` | `uv sync --locked`、ruff（lint・format確認）、mypy、pyright |
| `test (ubuntu-latest)` / `test (windows-latest)` | `uv sync --locked` と pytest |

- `uv sync --locked` は、`uv.lock` が `pyproject.toml` と食い違うと失敗する。依存を変えたら `uv.lock` も一緒にコミットする。
- Dependabot が毎週、Pythonの依存（`uv`）の更新PRを `develop` 向けに作る。開発用ツールの更新は1つのPRにまとめる。CIが通れば、更新でデグレしていないと判断できる。
- 必須チェックの設定は `git-setup/gh-RequiredCI.json` にある。GitHubへの反映は `git-setup/gh-enable-push-protection-win.bat` で行う。

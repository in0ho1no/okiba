# 開発ガイド

仕様は [spec.md](spec.md) を正本とする。本書は起動方法と、デグレを防ぐためのテストの書き方をまとめる。

## 起動

```bash
uv run python src/main.py
```

- `127.0.0.1` だけで待ち受ける。ポートは `--port` で変更できる（既定は8000）。
- HTTPのHostヘッダーは `127.0.0.1` と `localhost` のみ許可する。フェーズ4でLANから利用する際は、認証の追加とともに待ち受けアドレスと許可ホストを見直す。
- データ（SQLiteのDBと写真フォルダ）の保存先は、環境変数 `OKIBA_DATA_DIR` で指定する。未指定の場合はホームディレクトリの `okiba-data` を使う。
- 公開リポジトリに私物のデータが混入しないよう、保存先はリポジトリの外に置く。

## 構成

| パス | 内容 |
| --- | --- |
| `src/main.py` | 起動処理 |
| `src/okiba/migrations/` | DB形式の変更。`0001_initial.sql` から番号順に適用し、番号がDB形式のバージョンになる |
| `src/okiba/*.py` | 業務ロジック（カテゴリ、保管場所、物品、写真、検索、変更履歴、状態と削除、差し戻し、バックアップ） |
| `src/okiba/web/` | 画面（FastAPIのルーティング、Jinja2テンプレート、CSS・JavaScript） |
| `src/tests/` | テスト |

画面に `method="post"` のフォームを追加するときは、フォームの中に `<input type="hidden" name="csrf_token" value="{{ security.csrf_token }}">` を置く。トークンのない更新要求は、別サイトからの不正な送信（CSRF）として403で拒否する。`test_web.py` が、主な画面のすべてのPOSTフォームにトークンがあることを確認する。

CIのSemgrepでは、FastAPI/Jinjaのフォームに適用できないDjango専用のCSRFルールだけを除外する。フォームへのトークン埋め込みは `test_every_post_form_carries_token` で、更新要求の検証は `verify_token` のHTTPテストで確認する。

SQLは固定の文字列で書き、値はパラメーターで渡す。件数が変わる条件は、JSON配列を1つのパラメーターで渡して `json_each` で展開する。CIのSemgrepは、文字列を組み立てたSQLを指摘する。

DB形式を変えるときは、既存のSQLファイルを書き換えず、次の番号のSQLファイルを追加する。起動時に、更新前のDBを `okiba.v<旧バージョン>.bak.sqlite3` として退避してから適用する。

## バックアップと復元

バックアップは、管理画面の「バックアップ」で出力先フォルダを絶対パスで指定して作成する。DBと画像フォルダを `okiba-backup-YYYYMMDD-HHMMSS.zip` に書き出す。

- zipには、`manifest.json`（アプリとDB形式のバージョン、作成日時）、`okiba.sqlite3`、`photos/`（元画像・閲覧用画像・縮小版）が入る。
- 書き出しの間は、更新の要求を503で断り、閲覧だけを受け付ける。
- 書き出し中のファイル名には `.partial` を付ける。書き出したzipを開き直して、DBの整合性と、物品・履歴・写真の元画像を参照できることを確認する。確認に通った場合だけ正式な名前に変える。失敗した場合は書きかけのファイルを残さない。
- 元画像は画像として読み込めるかも確認する。読み込めない元画像は結果画面に警告として示すが、バックアップは完成させる。元の画像フォルダで既に壊れている画像があると、直すまでバックアップを取れなくなるためである。
- 写真フォルダの中は出力先にできない。前回のzipが次のバックアップに画像として入るためである。

復元は次の手順で行う。

1. Okibaを停止する。
2. バックアップのzipを展開する。
3. データの保存先（`OKIBA_DATA_DIR`、既定はホームディレクトリの `okiba-data`）の `okiba.sqlite3` と `photos` フォルダを別の場所へ移し、展開した同名のファイル・フォルダを置く。
4. Okibaを起動する。

起動時には、DB形式のバージョンを確認する。

- 古い形式なら、DBを退避してからマイグレーションする。
- アプリより新しい形式なら、理由を表示して起動しない。
- 閲覧用画像・縮小版が見つからない写真は、元画像から作り直す。

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

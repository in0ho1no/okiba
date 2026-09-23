---
applyTo: "src/**/*.py"
description: "Python ファイルを編集・作成・レビューするときに使う。pyproject準拠, コード品質, 仮想環境の補足ルール。"
---

- パッケージ追加は勝手に行わず、必要時はユーザー確認のうえ uv add を使う
- Python の実行や検証は uv run を使う
- ファイルパスの操作は os.path ではなく pathlib.Path を使う

- コーディング規約の正本は pyproject.toml とし、競合時はそちらを優先する
- 必要な変数や関数には型ヒントを付ける
- コード変更後は必ず `python-quality` スキルを使い、ruff・mypy・pyright の結果を確認する。

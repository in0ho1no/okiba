#!/bin/sh

set -eu

echo "============================================="
echo " GitHub Ruleset / Secret scanning / Auto-merge 有効化"
echo "============================================="
echo ""


# ---------------------------------------------------
# 目的: GitHub の RequiredCI Ruleset、Secret scanning、Push protection、Auto-merge 関連設定を有効化する。
# 概要: テンプレートから作成したリポジトリには設定が引き継がれないため、
#       リポジトリ作成後に一度実行する。gh CLI と管理者権限が必要。
# 補足: プライベートリポジトリでは GitHub Advanced Security (Secret Protection) の契約が必要。
# ---------------------------------------------------

if ! command -v gh >/dev/null 2>&1; then
  echo "[エラー] gh CLI が見つかりません。https://cli.github.com/ から導入してください。" >&2
  exit 1
fi

repo=$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)
if [ -z "$repo" ]; then
  echo "[エラー] リポジトリを特定できませんでした。GitHub リモートのあるリポジトリ内で実行してください。" >&2
  exit 1
fi

echo "[設定] $repo の dependencies ラベルを作成/更新します"
label_exists=$(gh api "repos/$repo/labels/dependencies" >/dev/null 2>&1 && printf '1' || true)

if [ -n "$label_exists" ]; then
  while :; do
    printf "dependencies ラベルは既に存在します。色と説明を上書きしますか? [y/n] "
    read -r answer
    case "$answer" in
      y|Y)
        if ! gh label create dependencies --repo "$repo" --color 0366d6 --description "Dependabot update" --force; then
          echo "[エラー] dependencies ラベルの更新に失敗しました。リポジトリの管理者権限があるか確認してください。" >&2
          exit 1
        fi
        break
        ;;
      n|N|"")
        echo "[スキップ] dependencies ラベルの更新を見送りました。"
        break
        ;;
      *)
        echo "y か n で入力してください。" >&2
        ;;
    esac
  done
else
  if ! gh label create dependencies --repo "$repo" --color 0366d6 --description "Dependabot update"; then
    echo "[エラー] dependencies ラベルの作成に失敗しました。リポジトリの管理者権限があるか確認してください。" >&2
    exit 1
  fi
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ruleset_file="$script_dir/gh-RequiredCI.json"
if [ ! -f "$ruleset_file" ]; then
  echo "[エラー] Ruleset 定義が見つかりません: $ruleset_file" >&2
  exit 1
fi

# 一覧取得の失敗を握りつぶすと POST に進んで重複作成/422 になるため、ここで明示的に止める
if ! ruleset_id=$(gh api "repos/$repo/rulesets" \
  --jq '[.[] | select(.name == "RequiredCI" and .source_type == "Repository")][0].id // empty'); then
  echo "[エラー] Ruleset 一覧の取得に失敗しました。リポジトリの管理者権限があるか確認してください。" >&2
  exit 1
fi

if [ -n "$ruleset_id" ]; then
  echo "[設定] $repo の RequiredCI Ruleset を更新します"
  ruleset_endpoint="repos/$repo/rulesets/$ruleset_id"
  ruleset_method=PUT
else
  echo "[設定] $repo に RequiredCI Ruleset を作成します"
  ruleset_endpoint="repos/$repo/rulesets"
  ruleset_method=POST
fi

if ! gh api -X "$ruleset_method" "$ruleset_endpoint" --input "$ruleset_file" --silent; then
  echo "[エラー] RequiredCI Ruleset の設定に失敗しました。リポジトリの管理者権限があるか確認してください。" >&2
  exit 1
fi
ruleset_id=$(gh api "repos/$repo/rulesets" \
  --jq '[.[] | select(.name == "RequiredCI" and .source_type == "Repository")][0].id // empty' \
  2>/dev/null || true)

echo "[設定] $repo の Secret scanning / Push protection / Auto-merge 関連設定を有効化します"
if ! gh api -X PATCH "repos/$repo" --silent \
  -f "security_and_analysis[secret_scanning][status]=enabled" \
  -f "security_and_analysis[secret_scanning_push_protection][status]=enabled" \
  -f "allow_auto_merge=true" \
  -f "allow_squash_merge=true" \
  -f "delete_branch_on_merge=true"; then
  echo "[エラー] 有効化に失敗しました。リポジトリの管理者権限があるか確認してください。" >&2
  echo "         プライベートリポジトリでは GitHub Advanced Security (Secret Protection) の契約が必要です。" >&2
  exit 1
fi

echo "[確認] 現在の設定:"
gh api "repos/$repo" --jq '{allow_auto_merge, allow_squash_merge, delete_branch_on_merge, security_and_analysis: .security_and_analysis | {secret_scanning, secret_scanning_push_protection}}'
if [ -n "$ruleset_id" ]; then
  gh api "repos/$repo/rulesets/$ruleset_id" --jq '{name, enforcement, conditions, rules}'
fi

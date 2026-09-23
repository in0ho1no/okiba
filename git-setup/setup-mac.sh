#!/bin/sh

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/.." && pwd)

if [ ! -e "$repo_root/.git" ]; then
    echo "[エラー] このスクリプトの親ディレクトリに .git が見つからないため、セットアップを中止します。" >&2
    exit 1
fi

if ! git -C "$repo_root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "[エラー] このスクリプトの親ディレクトリが Git リポジトリではないため、セットアップを中止します。" >&2
    exit 1
fi

# 各設定の実行用ヘルパー。
# 第1引数はラベル、残りは実行コマンド。
# 成功時のみ [設定] を表示し、失敗してもスクリプトは中断せずエラーを表示して次へ進む。
run() {
    label="$1"
    shift
    if "$@"; then
        echo "[設定] $label"
    else
        echo "[エラー] 設定に失敗しました: $label" >&2
    fi
}

echo "============================================="
echo " Git ローカル設定セットアップ"
echo "============================================="
echo ""


# ---------------------------------------------------
# 目的: コミットメッセージのテンプレートを設定する。
# 概要: git commit時にエディタへテンプレを表示するため。
# ---------------------------------------------------
run "コミットテンプレート" git -C "$repo_root" config --local commit.template git-setup/COMMIT_TEMPLATE


# ---------------------------------------------------
# 目的: リポジトリ管理のGit hooksを有効化する。
# 概要: commit-msgなどの共通フックを全員で共有するため。
# ---------------------------------------------------
run "core.hooksPath" git -C "$repo_root" config --local core.hooksPath git-setup/hooks
chmod +x "$repo_root/git-setup/hooks/commit-msg" 2>/dev/null || true
default_hooks_dir="$(git -C "$repo_root" rev-parse --absolute-git-dir)/hooks"
mkdir -p "$default_hooks_dir"
cat > "$default_hooks_dir/SETUP_CREATED_core.hooksPath_changed.txt" <<'EOF'
このリポジトリでは setup により core.hooksPath を git-setup/hooks に設定しています。
標準の hooks ディレクトリ配下のフックは通常参照されません。
フックを追加・変更する場合は git-setup/hooks を編集してください。
EOF


# ---------------------------------------------------
# 目的: fetch時にリモートで削除済みのブランチをローカルからも削除する。
# 概要: ブランチの扱いで混乱が生じるのを避けるため。
# ---------------------------------------------------
run "fetch.prune" git -C "$repo_root" config --local fetch.prune true


# ---------------------------------------------------
# 目的: pull.rebaseの設定を削除してデフォルト状態に戻す。
# 概要: pull.ff=onlyと組み合わせて、fast-forward以外のpullを抑止するため。
# ---------------------------------------------------
git -C "$repo_root" config --local --unset pull.rebase 2>/dev/null || true
echo "[設定] pull.rebase"


# ---------------------------------------------------
# 目的: git pull時にfast-forwardのみを許可する。
# 概要: マージコミットの生成を防ぎ、履歴をシンプルに保つため。
# ---------------------------------------------------
run "pull.ff" git -C "$repo_root" config --local pull.ff only


# ---------------------------------------------------
# 目的: git merge時にfast-forwardを行わず、必ずマージコミットを作成する。
# 概要: ブランチ単位の作業履歴を明確に残すため。
# ---------------------------------------------------
run "merge.ff" git -C "$repo_root" config --local merge.ff false


# ---------------------------------------------------
# 目的: 改行コードを自動変換しない。
# 概要: .gitattributesにより厳密に制御しているため。
# ---------------------------------------------------
run "core.autocrlf" git -C "$repo_root" config --local core.autocrlf false


# ---------------------------------------------------
# 目的: CRLFとLFが混じったテキストファイルのコミットに警告を出す。
# 概要: CRLFからLFへの変換でファイルが破損するリスクを抑える。
# 補足: 完全禁止は開発が止まりかねないのでtrueではなくwarnとする。
# ---------------------------------------------------
run "core.safecrlf" git -C "$repo_root" config --local core.safecrlf warn


echo ""
echo "============================================="
echo " セットアップが完了しました"
echo "============================================="

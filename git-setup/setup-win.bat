@echo off
chcp 65001 > nul
setlocal
set "HAS_ERROR="
set "FATAL_EXIT_CODE=1"

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "REPO_ROOT=%%~fI"

if not exist "%REPO_ROOT%\.git" (
    call :fatal_error "このスクリプトの親ディレクトリに .git が見つからないため、セットアップを中止します。" "1" "%REPO_ROOT%"
    exit /b %ERRORLEVEL%
)

git -C "%REPO_ROOT%" rev-parse --is-inside-work-tree > nul 2>&1
if errorlevel 1 (
    call :fatal_error "このスクリプトの親ディレクトリが Git リポジトリではないため、セットアップを中止します。" "1" "%REPO_ROOT%"
    exit /b %ERRORLEVEL%
)

echo =============================================
echo  Git ローカル設定セットアップ
echo =============================================
echo.


rem "---------------------------------------------------"
rem "目的: コミットメッセージのテンプレートを設定する。"
rem "概要: git commit時にエディタへテンプレを表示するため。"
rem "---------------------------------------------------"
call :run git -C "%REPO_ROOT%" config --local commit.template git-setup/COMMIT_TEMPLATE && echo [設定] コミットテンプレート


rem "---------------------------------------------------"
rem "目的: リポジトリ管理のGit hooksを有効化する。"
rem "概要: commit-msgなどの共通フックを全員で共有するため。"
rem "---------------------------------------------------"
call :run git -C "%REPO_ROOT%" config --local core.hooksPath git-setup/hooks && set HOOKS_OK=1
for /f "delims=" %%d in ('git -C "%REPO_ROOT%" rev-parse --absolute-git-dir') do set DEFAULT_HOOKS_DIR=%%d\hooks
if not exist "%DEFAULT_HOOKS_DIR%" mkdir "%DEFAULT_HOOKS_DIR%"
call :run powershell -NoProfile -Command "@('このリポジトリでは setup により core.hooksPath を git-setup/hooks に設定しています。','標準の hooks ディレクトリ配下のフックは通常参照されません。','フックを追加・変更する場合は git-setup/hooks を編集してください。') | Set-Content -Path '%DEFAULT_HOOKS_DIR%\SETUP_CREATED_core.hooksPath_changed.txt' -Encoding utf8"
if defined HOOKS_OK echo [設定] core.hooksPath


rem "---------------------------------------------------"
rem "目的: fetch時にリモートで削除済みのブランチをローカルからも削除する。"
rem "概要: ブランチの扱いで混乱が生じるのを避けるため。"
rem "---------------------------------------------------"
call :run git -C "%REPO_ROOT%" config --local fetch.prune true && echo [設定] fetch.prune


rem "---------------------------------------------------"
rem "目的: pull.rebaseの設定を削除してデフォルト状態に戻す。"
rem "概要: pull.ff=onlyと組み合わせて、fast-forward以外のpullを抑止するため。"
rem "---------------------------------------------------"
git -C "%REPO_ROOT%" config --local --unset pull.rebase > nul 2> nul
echo [設定] pull.rebase


rem "---------------------------------------------------"
rem "目的: git pull時にfast-forwardのみを許可する。"
rem "概要: マージコミットの生成を防ぎ、履歴をシンプルに保つため。"
rem "---------------------------------------------------"
call :run git -C "%REPO_ROOT%" config --local pull.ff only && echo [設定] pull.ff


rem "---------------------------------------------------"
rem "目的: git merge時にfast-forwardを行わず、必ずマージコミットを作成する。"
rem "概要: ブランチ単位の作業履歴を明確に残すため。"
rem "---------------------------------------------------"
call :run git -C "%REPO_ROOT%" config --local merge.ff false && echo [設定] merge.ff


rem "---------------------------------------------------"
rem "目的: 改行コードを自動変換しない。"
rem "概要: .gitattributesにより厳密に制御しているため。"
rem "---------------------------------------------------"
call :run git -C "%REPO_ROOT%" config --local core.autocrlf false && echo [設定] core.autocrlf


rem "---------------------------------------------------"
rem "目的: CRLFとLFが混じったテキストファイルのコミットに警告を出す。"
rem "概要: CRLFからLFへの変換でファイルが破損するリスクを抑える。"
rem "補足: 完全禁止は開発が止まりかねないのでtrueではなくwarnとする。"
rem "---------------------------------------------------"
call :run git -C "%REPO_ROOT%" config --local core.safecrlf warn && echo [設定] core.safecrlf


rem "---------------------------------------------------"
rem "目的: git windiffコマンドを使えるようにする。"
rem "概要: WinMergeによる差分比較ができるようにするため。"
rem "補足: デフォルトパスに見つからない場合はスキップする。"
rem "---------------------------------------------------"
set WINMERGE=C:\Program Files\WinMerge\WinMergeU.exe
if exist "%WINMERGE%" (
    call :run git -C "%REPO_ROOT%" config --local diff.tool WinMerge
    call :run git -C "%REPO_ROOT%" config --local difftool.prompt false
    call :run git -C "%REPO_ROOT%" config --local difftool.WinMerge.cmd "\"C:/Program Files/WinMerge/WinMergeU.exe\" -e -r -u -x -wl -wr -dl \"a/$MERGED\" -dr \"b/$MERGED\" \"$LOCAL\" \"$REMOTE\""
    call :run git -C "%REPO_ROOT%" config --local difftool.WinMerge.trustExitCode false
    call :run git -C "%REPO_ROOT%" config --local alias.windiff "difftool -y -d -t WinMerge" && echo [設定] WinMerge    ^(git windiff が使用可能です^)
) else (
    echo git windiffコマンドの設定は行いませんでした。（スキップ）
)

echo.
echo =============================================
if defined HAS_ERROR (
    echo  セットアップは完了しました（一部エラーあり）
) else (
    echo  セットアップが完了しました
)
echo =============================================
echo.
if defined HAS_ERROR (
    echo [確認] エラー内容を見直してください。
    echo.
)
pause
exit /b 0

rem "失敗しても中断せず、エラーを表示して次の設定へ進む。"
rem "成功時は errorlevel 0 を返し、呼び出し側で && により [設定] を表示する。"
:run
%*
if errorlevel 1 (
    set "HAS_ERROR=1"
    call :print_error "設定に失敗しました: %*" "%ERRORLEVEL%"
    exit /b 1
)
exit /b 0

:print_error
echo [エラー] %~1
if not "%~2"=="" echo [エラー詳細] 終了コード: %~2
exit /b 0

:fatal_error
call :print_error "%~1" "%~2"
if not "%~3"=="" echo [実行場所] %~3
echo.
echo [中止] Enterキーで終了します。
set "FATAL_EXIT_CODE=%~2"
goto :abort

:abort
pause
exit /b %FATAL_EXIT_CODE%

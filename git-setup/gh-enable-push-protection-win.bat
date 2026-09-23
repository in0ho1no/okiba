@echo off
chcp 65001 > nul
setlocal

echo =============================================
echo  GitHub Ruleset / Secret scanning / Auto-merge 有効化
echo =============================================
echo.


rem "---------------------------------------------------"
rem "目的: GitHub の RequiredCI Ruleset、Secret scanning、Push protection、Auto-merge 関連設定を有効化する。"
rem "概要: テンプレートから作成したリポジトリには設定が引き継がれないため、"
rem "      リポジトリ作成後に一度実行する。gh CLI と管理者権限が必要。"
rem "補足: プライベートリポジトリでは GitHub Advanced Security (Secret Protection) の契約が必要。"
rem "---------------------------------------------------"

where gh > nul 2> nul
if errorlevel 1 (
  echo [エラー] gh CLI が見つかりません。https://cli.github.com/ から導入してください。
  pause
  exit /b 1
)

set REPO=
for /f "delims=" %%r in ('gh repo view --json nameWithOwner -q .nameWithOwner 2^>nul') do set REPO=%%r
if not defined REPO (
  echo [エラー] リポジトリを特定できませんでした。GitHub リモートのあるリポジトリ内で実行してください。
  pause
  exit /b 1
)

echo [設定] %REPO% の dependencies ラベルを作成/更新します
set LABEL_EXISTS=
gh api "repos/%REPO%/labels/dependencies" >nul 2>nul
if not errorlevel 1 set LABEL_EXISTS=1

if defined LABEL_EXISTS (
  choice /c YN /m "dependencies ラベルは既に存在します。色と説明を上書きしますか?"
  if errorlevel 2 (
    echo [スキップ] dependencies ラベルの更新を見送りました。
  ) else (
    gh label create dependencies --repo "%REPO%" --color 0366d6 --description "Dependabot update" --force
    if errorlevel 1 (
      echo [エラー] dependencies ラベルの更新に失敗しました。リポジトリの管理者権限があるか確認してください。
      pause
      exit /b 1
    )
  )
) else (
  gh label create dependencies --repo "%REPO%" --color 0366d6 --description "Dependabot update"
  if errorlevel 1 (
    echo [エラー] dependencies ラベルの作成に失敗しました。リポジトリの管理者権限があるか確認してください。
    pause
    exit /b 1
  )
)

set RULESET_FILE=%~dp0gh-RequiredCI.json
if not exist "%RULESET_FILE%" (
  echo [エラー] Ruleset 定義が見つかりません: %RULESET_FILE%
  pause
  exit /b 1
)

rem for /f 経由では ^| のキャレットが jq 式に混入し検出が失敗するため、一時ファイル経由で受け取る
set RULESET_TMP=%TEMP%\gh-ruleset-id.tmp
set RULESET_ID=
gh api "repos/%REPO%/rulesets" --jq "[.[] | select(.name == \"RequiredCI\" and .source_type == \"Repository\")][0].id // empty" > "%RULESET_TMP%"
if errorlevel 1 (
  del "%RULESET_TMP%" > nul 2> nul
  echo [エラー] Ruleset 一覧の取得に失敗しました。リポジトリの管理者権限があるか確認してください。
  pause
  exit /b 1
)
set /p RULESET_ID=<"%RULESET_TMP%"
del "%RULESET_TMP%" > nul 2> nul

if defined RULESET_ID (
  echo [設定] %REPO% の RequiredCI Ruleset を更新します
  gh api -X PUT "repos/%REPO%/rulesets/%RULESET_ID%" --input "%RULESET_FILE%" --silent
) else (
  echo [設定] %REPO% に RequiredCI Ruleset を作成します
  gh api -X POST "repos/%REPO%/rulesets" --input "%RULESET_FILE%" --silent
)
if errorlevel 1 (
  echo [エラー] RequiredCI Ruleset の設定に失敗しました。リポジトリの管理者権限があるか確認してください。
  pause
  exit /b 1
)
gh api "repos/%REPO%/rulesets" --jq "[.[] | select(.name == \"RequiredCI\" and .source_type == \"Repository\")][0].id // empty" > "%RULESET_TMP%" 2> nul
set RULESET_ID=
set /p RULESET_ID=<"%RULESET_TMP%"
del "%RULESET_TMP%" > nul 2> nul

echo [設定] %REPO% の Secret scanning / Push protection / Auto-merge 関連設定を有効化します
gh api -X PATCH "repos/%REPO%" --silent ^
  -f "security_and_analysis[secret_scanning][status]=enabled" ^
  -f "security_and_analysis[secret_scanning_push_protection][status]=enabled" ^
  -f "allow_auto_merge=true" ^
  -f "allow_squash_merge=true" ^
  -f "delete_branch_on_merge=true"
if errorlevel 1 (
  echo [エラー] 有効化に失敗しました。リポジトリの管理者権限があるか確認してください。
  echo          プライベートリポジトリでは GitHub Advanced Security ^(Secret Protection^) の契約が必要です。
  pause
  exit /b 1
)

echo [確認] 現在の設定:
gh api "repos/%REPO%" --jq "{allow_auto_merge, allow_squash_merge, delete_branch_on_merge, security_and_analysis: .security_and_analysis | {secret_scanning, secret_scanning_push_protection}}"
if defined RULESET_ID gh api "repos/%REPO%/rulesets/%RULESET_ID%" --jq "{name, enforcement, conditions, rules}"

echo.
pause

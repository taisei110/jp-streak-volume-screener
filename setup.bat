@echo off
chcp 65001 > nul
setlocal
cd /d "%~dp0"

echo ======================================
echo   連騰スクリーナー かんたんセットアップ
echo ======================================
echo.

echo [1/3] 必要なライブラリをインストールします...
pip install -r requirements-cli.txt
echo.

echo [2/3] Discord通知の設定（不要ならそのままEnter）
set "WEBHOOK="
set /p "WEBHOOK=Discord Webhook URLを貼り付けてEnter: "
if not "%WEBHOOK%"=="" (
    setx DISCORD_WEBHOOK_URL "%WEBHOOK%" > nul
    echo   Discord通知を設定しました。
) else (
    echo   通知なしで進めます。
)
echo.

echo [3/3] 毎日20時の自動実行を登録します...
schtasks /create /tn "連騰スクリーナー" /sc daily /st 20:00 /f /tr "\"%~dp0run_daily.bat\""
echo.

echo ======================================
echo   完了しました。
echo.
echo   毎日20時に自動で動きます
echo   結果は screen_result.html に出ます
echo   今すぐ試すには run_now.bat をダブルクリック
echo ======================================
echo.
pause

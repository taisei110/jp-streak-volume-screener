@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo スクリーナーを実行します...
python jp_streak_volume_screener.py
echo.
echo 結果を開きます...
start "" "screen_result.html"
echo.
pause

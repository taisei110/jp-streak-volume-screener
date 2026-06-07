@echo off
chcp 65001 > nul
REM ============================================================
REM 連騰スクリーナー 自動実行ラッパー（タスクスケジューラから呼ばれる）
REM Discord Webhook URL はユーザー環境変数 DISCORD_WEBHOOK_URL から読み込む。
REM 設定方法（コマンドプロンプト/PowerShell、1回だけ）:
REM   setx DISCORD_WEBHOOK_URL "https://discord.com/api/webhooks/..."
REM ※ソースやこのbatに直書きしないこと（秘密情報のため）。
REM ============================================================
cd /d "%~dp0"
"C:\Users\taise\AppData\Local\Programs\Python\Python312\python.exe" -u jp_streak_volume_screener.py > scheduler_log.txt 2>&1

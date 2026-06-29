@echo off
rem Launch the streak screener app (works from any directory)
cd /d "%~dp0"
streamlit run app.py

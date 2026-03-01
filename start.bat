@echo off
chcp 65001 >nul
set PYTHONPYCACHEPREFIX=.cache\pycache
set PYTHONUTF8=1
venv\Scripts\python main.py %*

@echo off
chcp 65001 >nul
set PYTHONUTF8=1
if not exist .venv\Lib\site-packages\sitecustomize.py (
    .venv\Scripts\python -c "from pathlib import Path; Path('.venv/Lib/site-packages/sitecustomize.py').write_text('import sys, os\nsys.pycache_prefix = os.path.join(sys.prefix, \"__pycache__\")\n')"
)
.venv\Scripts\python main.py %*

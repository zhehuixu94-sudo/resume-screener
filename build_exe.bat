@echo off
REM 一键打包 exe：双击即可（需要先建好 .venv 并装依赖，见 README）
chcp 65001 >nul
cd /d "%~dp0"

echo [1/3] 检查依赖
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, psycopg, pymupdf, PyInstaller" || (
  echo 依赖不全，先执行：
  echo   uv pip install --python .venv\Scripts\python.exe pydantic jsonschema pymupdf python-docx pytest fastapi "uvicorn[standard]" python-multipart pyinstaller "psycopg[binary]"
  pause & exit /b 1
)

echo [2/3] 打包（单文件，约 1 分钟）
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --name ResumeScreener ^
  --add-data "web;web" --add-data "config;config" --add-data "schemas;schemas" ^
  --hidden-import uvicorn.logging --hidden-import uvicorn.loops.auto ^
  --hidden-import uvicorn.protocols.http.auto --hidden-import uvicorn.protocols.websockets.auto ^
  --hidden-import uvicorn.lifespan.on ^
  --collect-submodules psycopg --collect-submodules psycopg_binary --collect-submodules pymupdf ^
  --exclude-module tkinter --exclude-module matplotlib launcher.py
if errorlevel 1 ( echo 打包失败 & pause & exit /b 1 )

echo [3/3] 完成：dist\ResumeScreener.exe（双击运行，会自动打开浏览器）
dir dist\ResumeScreener.exe
pause

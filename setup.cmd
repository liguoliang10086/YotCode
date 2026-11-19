@echo off
setlocal
REM YotCode one-shot setup for Windows.
REM Creates .venv, installs dependencies and the package in editable mode.

cd /d "%~dp0"

REM The official PyPI can drop TLS connections on some networks, which shows
REM up as "SSL: UNEXPECTED EOF WHILE READING" and breaks the build-isolation
REM step that installs setuptools>=77. Use a stable mirror by default.
REM Override with: set YOTCODE_PIP_INDEX=https://pypi.org/simple
if not defined YOTCODE_PIP_INDEX set "YOTCODE_PIP_INDEX=%CLAUDEPY_PIP_INDEX%"
if not defined YOTCODE_PIP_INDEX set "YOTCODE_PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple"
if not defined YOTCODE_PIP_TRUSTED_HOST set "YOTCODE_PIP_TRUSTED_HOST=%CLAUDEPY_PIP_TRUSTED_HOST%"
if not defined YOTCODE_PIP_TRUSTED_HOST set "YOTCODE_PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn"
set "PIP_INDEX_URL=%YOTCODE_PIP_INDEX%"
set "PIP_TRUSTED_HOST=%YOTCODE_PIP_TRUSTED_HOST%"
set "PIP_RETRIES=10"
set "PIP_DEFAULT_TIMEOUT=60"
echo [yot] Using PyPI index: %PIP_INDEX_URL%

if not exist ".venv\Scripts\python.exe" (
    echo [yot] Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo [yot] Failed to create .venv. Is Python 3.10+ on PATH?
        exit /b 1
    )
)

echo [yot] Installing package in editable mode...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 (
    echo [yot] Failed to upgrade pip.
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install -e ".[dev]"
if errorlevel 1 (
    echo [yot] Installation failed.
    exit /b 1
)

echo.
echo [yot] Done. Run the agent with:
echo     run.cmd
echo or:
echo     .venv\Scripts\yot.exe --help
echo.
echo Configure an API key first, e.g.:
echo     set ANTHROPIC_API_KEY=sk-ant-...
echo     copy .env.example .env      (then edit .env in your editor)
endlocal

@echo off
setlocal
REM Launch YotCode. Extra arguments are passed straight to the CLI, e.g.
REM     run.cmd --mock
REM     run.cmd -p "list the files in this repo"
REM     run.cmd --provider openai --model deepseek-chat

REM Prefer Windows Terminal when available: it renders every glyph and avoids
REM the classic conhost redraw artifacts (ghost text below the input box).
REM WT_SESSION is defined when already running inside Windows Terminal.
if not defined WT_SESSION (
    where wt >nul 2>nul && (
        start "" wt --maximize -d "%~dp0" cmd /c ""%~f0" %*"
        exit /b
    )
    echo [yot] Tip: classic cmd.exe windows can show ghost text / stray cursors.
    echo [yot] Install Windows Terminal for the best experience:
    echo [yot]     winget install Microsoft.WindowsTerminal
)

REM Switch the console to UTF-8 so Chinese text and Unicode symbols render
REM correctly in the classic cmd.exe window (not just Windows Terminal).
chcp 65001 >nul 2>&1

REM Clear the screen so the TUI anchors at the top of the console buffer.
REM Without this, classic cmd.exe windows offset the whole UI by a few rows:
REM the logo gets clipped at the top and stale rows appear near the prompt.
cls

cd /d "%~dp0"

if exist ".venv\Scripts\yot.exe" (
    ".venv\Scripts\yot.exe" %*
) else if exist ".venv\Scripts\yotcode.exe" (
    ".venv\Scripts\yotcode.exe" %*
) else if exist ".venv\Scripts\python.exe" (
    set "PYTHONPATH=%~dp0"
    ".venv\Scripts\python.exe" -m yotcode %*
) else (
    echo [yot] No virtual environment found. Running setup.cmd first...
    call setup.cmd
    if errorlevel 1 exit /b 1
    ".venv\Scripts\yotcode.exe" %*
)

endlocal

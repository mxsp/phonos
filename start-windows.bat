@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\phonos.exe" (
    ".venv\Scripts\phonos.exe" production %*
    goto :eof
)
if exist "venv\Scripts\phonos.exe" (
    "venv\Scripts\phonos.exe" production %*
    goto :eof
)

echo First-run detected: launching Phonos Guided Browser Installer...
python scripts\install-ui.py %*

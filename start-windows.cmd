@echo off
setlocal
if exist "%~dp0Mute.exe" (
    start "" "%~dp0Mute.exe" settings
    exit /b 0
)
py -3.14 "%~dp0run.py" settings
if errorlevel 1 (
    echo Mute could not start. Use the Windows download with Mute.exe, or install Python 3.14 with Tk.
    pause
    exit /b 1
)

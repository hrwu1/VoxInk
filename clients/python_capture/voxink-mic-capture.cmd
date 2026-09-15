@echo off
rem The portable microphone capture backend on Windows. The transcription
rem helper launches it when the native Windows backend's packages are not
rem installed; it takes pause/resume/stop lines on stdin.
setlocal
set "PROJECT_DIR=%~dp0..\.."
if not exist "%PROJECT_DIR%\.venv\Scripts\python.exe" (
    echo VoxInk requires a Python 3.12 environment at %PROJECT_DIR%\.venv. 1>&2
    echo Create it with: py -3.12 -m venv .venv 1>&2
    exit /b 2
)
"%PROJECT_DIR%\.venv\Scripts\python.exe" -m clients.python_capture %*
exit /b %ERRORLEVEL%

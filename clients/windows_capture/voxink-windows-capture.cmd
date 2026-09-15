@echo off
rem The native Windows capture backend. The transcription helper launches it
rem on Windows in place of the Swift voxink-capture; both speak the same
rem command line, and this one takes pause/resume/stop lines on stdin.
setlocal
set "PROJECT_DIR=%~dp0..\.."
if not exist "%PROJECT_DIR%\.venv\Scripts\python.exe" (
    echo VoxInk requires a Python 3.12 environment at %PROJECT_DIR%\.venv. 1>&2
    echo Create it with: py -3.12 -m venv .venv 1>&2
    exit /b 2
)
"%PROJECT_DIR%\.venv\Scripts\python.exe" -m clients.windows_capture %*
exit /b %ERRORLEVEL%

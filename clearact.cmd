@echo off
setlocal
pushd "%~dp0"
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
set "PYTHONUTF8=1"
set "CLEARACT_PYTHON=python"
if exist "%~dp0.venv\Scripts\python.exe" set "CLEARACT_PYTHON=%~dp0.venv\Scripts\python.exe"
if "%~1"=="" (
    "%CLEARACT_PYTHON%" -m clearact_lite web --open
) else (
    "%CLEARACT_PYTHON%" -m clearact_lite %*
)
set "CLEARACT_EXIT=%ERRORLEVEL%"
if not "%CLEARACT_EXIT%"=="0" (
    echo.
    echo ClearAct could not start or the command failed. See the error above.
    echo To install dependencies: python -m pip install -e .
    pause
)
popd
exit /b %CLEARACT_EXIT%

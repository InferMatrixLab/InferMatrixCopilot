@echo off
setlocal

for %%A in (%*) do if "%%~A"=="--dry-run" goto dryrun

where uv >nul 2>nul
if errorlevel 1 (
  echo Installing the small uv runtime...
  powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "irm https://astral.sh/uv/install.ps1 | iex"
  set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)

where uv >nul 2>nul
if errorlevel 1 (
  echo Error: uv installation failed.
  exit /b 1
)

uv run --no-project "%~dp0scripts\install_mcp.py" %*
exit /b %ERRORLEVEL%

:dryrun
where py >nul 2>nul
if not errorlevel 1 goto dryrun_py
where python >nul 2>nul
if not errorlevel 1 goto dryrun_python
echo Dry run requires Python 3.11 or later; no changes were made.
exit /b 1

:dryrun_py
py -3 "%~dp0scripts\install_mcp.py" %*
exit /b %ERRORLEVEL%

:dryrun_python
python "%~dp0scripts\install_mcp.py" %*
exit /b %ERRORLEVEL%

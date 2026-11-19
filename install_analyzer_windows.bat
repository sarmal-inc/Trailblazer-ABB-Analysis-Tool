@echo off
rem ======================================================================
rem  analysis_tools installer for Windows
rem
rem  Put this file in the analysis_tool folder and run it from cmd
rem  (or double-click it). It will:
rem    1. find Python 3.9+ with tkinter
rem    2. create a venv in %USERPROFILE%\venvs\analysis_tools
rem       (outside OneDrive, so OneDrive doesn't sync thousands of files)
rem    3. install numpy, pandas, matplotlib, openpyxl, tkinterdnd2
rem    4. create run_analyzer.bat and a desktop shortcut
rem
rem  Options:
rem    install_analyzer.bat /force        delete and rebuild the venv
rem    install_analyzer.bat /noshortcut   skip the desktop shortcut
rem ======================================================================
setlocal
title analysis_tools installer

set "APP_DIR=%~dp0"
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"
set "VENV_DIR=%USERPROFILE%\venvs\analysis_tools"
set "VPY=%VENV_DIR%\Scripts\python.exe"
set "MAIN=multi_file_analyzer.py"
set "PACKAGES=numpy pandas matplotlib openpyxl tkinterdnd2"

set "FORCE="
set "NOSHORTCUT="
:parse_args
if "%~1"=="" goto :args_done
if /i "%~1"=="/force" set "FORCE=1"
if /i "%~1"=="/noshortcut" set "NOSHORTCUT=1"
shift
goto :parse_args
:args_done

echo.
echo  Installing analysis_tools
echo  App folder: %APP_DIR%
echo  Venv:       %VENV_DIR%
echo.

if not exist "%APP_DIR%\%MAIN%" (
    echo  WARNING: %MAIN% not found next to this installer.
    echo           Move install_analyzer.bat into the analysis_tool folder.
    echo.
)

rem ---------------------------------------------------------------- 1 ---
echo [1/5] Looking for Python...
set "PY="
py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys" >nul 2>&1 && set "PY=python"
if not defined PY goto :no_python

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"
if errorlevel 1 goto :old_python
for /f "delims=" %%v in ('%PY% --version 2^>^&1') do echo       Found %%v

rem ---------------------------------------------------------------- 2 ---
echo [2/5] Checking tkinter...
%PY% -c "import tkinter" >nul 2>&1
if errorlevel 1 goto :no_tk
echo       tkinter OK

rem ---------------------------------------------------------------- 3 ---
echo [3/5] Setting up the virtual environment...
if defined FORCE if exist "%VENV_DIR%" (
    echo       Removing old venv
    rmdir /s /q "%VENV_DIR%"
)
if exist "%VPY%" (
    echo       Venv already exists, reusing it
    goto :venv_ready
)
%PY% -m venv "%VENV_DIR%"
if errorlevel 1 goto :fail
echo       Created
:venv_ready

rem ---------------------------------------------------------------- 4 ---
echo [4/5] Installing packages: %PACKAGES%
"%VPY%" -m pip install --upgrade pip --disable-pip-version-check --quiet
"%VPY%" -m pip install --upgrade --disable-pip-version-check %PACKAGES%
if errorlevel 1 goto :pip_fail

"%VPY%" -c "import numpy, pandas, matplotlib, openpyxl, tkinter; matplotlib.use('TkAgg'); from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg; print('      numpy', numpy.__version__, '| pandas', pandas.__version__, '| matplotlib', matplotlib.__version__, '| openpyxl', openpyxl.__version__)"
if errorlevel 1 goto :fail
for %%f in (multi_file_analyzer.py trace_store.py chart_tools.py noise.py settings.py smoothing_function.py) do (
    if not exist "%APP_DIR%\%%f" echo       WARNING: %%f is missing from the app folder; the analyzer will not start without it.
)
"%VPY%" -c "import tkinterdnd2" >nul 2>&1 || echo       Note: tkinterdnd2 unavailable, drag and drop will be disabled.

rem ---------------------------------------------------------------- 5 ---
echo [5/5] Creating launcher...
(
    echo @echo off
    echo cd /d "%%~dp0"
    echo "%%USERPROFILE%%\venvs\analysis_tools\Scripts\python.exe" %MAIN%
    echo if errorlevel 1 pause
) > "%APP_DIR%\run_analyzer.bat"
echo       Created run_analyzer.bat

if defined NOSHORTCUT goto :done
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s = (New-Object -ComObject WScript.Shell).CreateShortcut([IO.Path]::Combine([Environment]::GetFolderPath('Desktop'), 'Multi File Analyzer.lnk')); $s.TargetPath = '%APP_DIR%\run_analyzer.bat'; $s.WorkingDirectory = '%APP_DIR%'; $s.Save()" >nul 2>&1
if errorlevel 1 (
    echo       Could not create desktop shortcut, use run_analyzer.bat instead
) else (
    echo       Created desktop shortcut "Multi File Analyzer"
)

:done
echo.
echo  Done! Start the tool with the desktop shortcut or run_analyzer.bat.
echo.
pause
exit /b 0

rem ------------------------------------------------------------ errors ---
:no_python
echo.
echo  ERROR: Python was not found.
echo  Install Python 3.11 or 3.12 from https://www.python.org/downloads/
echo  and tick "Add python.exe to PATH" in the installer, then run this again.
goto :fail_end

:old_python
echo.
echo  ERROR: Python 3.9 or newer is required.
echo  Install a newer version from https://www.python.org/downloads/
goto :fail_end

:no_tk
echo.
echo  ERROR: tkinter is missing from your Python install.
echo  Re-run the python.org installer, choose Modify, and tick "tcl/tk and IDLE".
goto :fail_end

:pip_fail
echo.
echo  ERROR: Package install failed.
echo  If you are on a company network, pip may be blocked by a proxy or firewall.
echo  Ask IT about pip access, or set HTTPS_PROXY before running this script.
goto :fail_end

:fail
echo.
echo  ERROR: Setup failed, see the messages above.

:fail_end
echo.
pause
exit /b 1
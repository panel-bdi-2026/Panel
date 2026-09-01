@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_CMD=python"
where python >nul 2>nul
if errorlevel 1 (
    where py >nul 2>nul
    if errorlevel 1 (
        echo No se encontro Python instalado.
        echo Instalalo desde https://www.python.org/downloads/ ^(marca la casilla "Add to PATH" durante la instalacion^) y volve a correr este archivo.
        pause
        exit /b 1
    )
    set "PYTHON_CMD=py"
)

if not exist .venv (
    echo Preparando el entorno por primera vez, esto puede tardar un minuto...
    %PYTHON_CMD% -m venv .venv
)

call .venv\Scripts\activate.bat

pip install -q -r requirements.txt
pip install -q qrcode

python start_dashboard.py

echo.
echo El dashboard se detuvo.
pause

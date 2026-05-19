@echo off
setlocal enabledelayedexpansion

where pyinstaller >nul 2>nul
if errorlevel 1 (
  echo [ERROR] PyInstaller non trovato. Installa con: pip install pyinstaller
  exit /b 1
)

set ROOT=%~dp0

echo [INFO] Build SoftiBridge_Sidecar.exe ...
pyinstaller ^
  --noconfirm ^
  --onefile ^
  --name SoftiBridge_Sidecar ^
  --console ^
  "%ROOT%sidecar.py"
if errorlevel 1 exit /b 1

echo [OK] Dist: %ROOT%dist\SoftiBridge_Sidecar.exe
echo Uso: SoftiBridge_Sidecar.exe   (richiede sidecar_config.json nella stessa cartella)
echo        oppure: set SOFTIBRIDGE_SIDECAR_CONFIG=C:\path\config.json ^&^& SoftiBridge_Sidecar.exe

endlocal

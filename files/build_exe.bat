@echo off
REM ============================================================
REM  Builds 4KWallpaperChanger.exe  (+ Setup.exe if Inno Setup is installed)
REM  Run this on Windows, in the folder that holds wallpaper_changer.py
REM ============================================================
setlocal
cd /d "%~dp0"

echo Closing any running copy of the app ...
taskkill /F /IM 4KWallpaperChanger.exe >nul 2>&1

echo [1/4] Installing dependencies ...
python -m pip install -r requirements.txt || goto :err

echo [2/4] Creating icon ...
python make_icon.py || goto :err

echo [3/4] Building exe (PyInstaller) ...
python -m PyInstaller --noconfirm --clean --windowed --onedir ^
  --name 4KWallpaperChanger --icon app.ico ^
  --collect-all customtkinter ^
  --hidden-import pystray._win32 ^
  wallpaper_changer.py || goto :err

echo [4/4] Building installer (Inno Setup) ...
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
if exist "%ISCC%" (
    "%ISCC%" installer.iss || goto :err
    echo.
    echo Done:  Output\4KWallpaperChanger_Setup.exe
) else (
    echo.
    echo Inno Setup 6 not found - install it from https://jrsoftware.org/isdl.php and run this again.
    echo The portable app is ready in:  dist\4KWallpaperChanger\4KWallpaperChanger.exe
)
pause
exit /b 0

:err
echo.
echo Build failed - see the messages above.
pause
exit /b 1

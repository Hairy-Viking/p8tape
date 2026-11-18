@echo off
rem Builds dist\p8tape.exe on a Windows PC.
rem Needs Python 3.12 from python.org (tick "Add python.exe to PATH").
python -m pip install pyinstaller numpy pillow sounddevice
if errorlevel 1 goto failed
python -m PyInstaller --onefile --windowed --name p8tape --add-data "loader.p8;." --hidden-import sounddevice --noconfirm p8tape.py
if errorlevel 1 goto failed
echo.
echo Done: dist\p8tape.exe
pause
exit /b 0
:failed
echo.
echo Build failed - see the messages above.
pause
exit /b 1

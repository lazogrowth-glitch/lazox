@echo off
echo.
echo ============================================
echo   VocalType - Creation du .exe
echo ============================================
echo.

echo Installation de PyInstaller...
pip install pyinstaller --quiet
echo.

echo Generation de l'icone...
python generate_icon.py
echo.

echo Fermeture de VocalType si ouvert...
taskkill /F /IM VocalType.exe /T 2>nul
timeout /t 2 /nobreak >nul
echo.

echo Creation du .exe (patiente 2-3 minutes)...
pyinstaller --noconfirm --onefile --windowed ^
    --icon=icon.ico ^
    --name=VocalType ^
    --add-data "icon.ico;." ^
    --hidden-import=speech_recognition ^
    --hidden-import=sounddevice ^
    --hidden-import=numpy ^
    --hidden-import=scipy ^
    --hidden-import=scipy.signal ^
    --hidden-import=scipy.io ^
    --hidden-import=scipy.io.wavfile ^
    --hidden-import=pyperclip ^
    --hidden-import=keyboard ^
    --hidden-import=PIL ^
    --hidden-import=PIL.Image ^
    --hidden-import=PIL.ImageDraw ^
    --hidden-import=PIL.ImageFont ^
    --hidden-import=pystray ^
    --hidden-import=pystray._win32 ^
    --hidden-import=tkinter ^
    --hidden-import=tkinter.ttk ^
    --hidden-import=tkinter.messagebox ^
    --hidden-import=tkinter.simpledialog ^
    --hidden-import=tkinter.scrolledtext ^
    --hidden-import=winreg ^
    --hidden-import=winsound ^
    --hidden-import=ctypes ^
    --collect-all=speech_recognition ^
    --collect-all=sounddevice ^
    dictation.py

echo.
echo ============================================
echo   DONE !
echo   Ton .exe est dans : dist\VocalType.exe
echo   Maintenant lance create_installer.bat
echo   pour mettre a jour VocalType_Setup.exe
echo ============================================
echo.
pause

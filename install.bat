@echo off
echo.
echo ============================================
echo   VocalType - Installation des dependances
echo ============================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERREUR: Python n'est pas installe sur ce PC.
    echo.
    echo  1. Va sur https://www.python.org/downloads/
    echo  2. Telecharge Python 3.11 ou plus recent
    echo  3. IMPORTANT : coche la case "Add Python to PATH"
    echo  4. Relance ce fichier install.bat
    echo.
    pause
    exit /b 1
)

echo Python detecte :
python --version
echo.

echo Mise a jour de pip...
python -m pip install --upgrade pip --quiet
echo.

echo Installation des modules en cours...
pip install SpeechRecognition sounddevice numpy scipy pyperclip keyboard Pillow pystray

if errorlevel 1 (
    echo.
    echo ERREUR lors de l'installation.
    echo Verifie ta connexion internet et reessaie.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   INSTALLATION REUSSIE !
echo   Lance maintenant start.bat
echo ============================================
echo.
pause

@echo off
echo.
echo ============================================
echo   VocalType - Creation de l'installateur
echo ============================================
echo.

REM Verifier que dist\VocalType.exe existe
if not exist "dist\VocalType.exe" (
    echo ERREUR : dist\VocalType.exe introuvable.
    echo Lance d'abord build.bat pour creer l'exe.
    echo.
    pause
    exit /b 1
)

REM Chercher Inno Setup dans les emplacements habituels
set ISCC=""
if exist "%PROGRAMFILES(X86)%\Inno Setup 6\ISCC.exe" set ISCC="%PROGRAMFILES(X86)%\Inno Setup 6\ISCC.exe"
if exist "%PROGRAMFILES%\Inno Setup 6\ISCC.exe"      set ISCC="%PROGRAMFILES%\Inno Setup 6\ISCC.exe"
if exist "%PROGRAMFILES(X86)%\Inno Setup 5\ISCC.exe" set ISCC="%PROGRAMFILES(X86)%\Inno Setup 5\ISCC.exe"

if %ISCC%=="" (
    echo Inno Setup n'est pas installe sur ton PC.
    echo.
    echo Telecharge-le gratuitement ici :
    echo   https://jrsoftware.org/isdl.php
    echo.
    echo Installe-le, puis relance ce fichier.
    pause
    exit /b 1
)

echo Compilation de l'installateur...
%ISCC% setup.iss

if %errorlevel%==0 (
    echo.
    echo ============================================
    echo   SUCCES !
    echo   L'installateur est pret :
    echo   VocalType_Setup.exe
    echo ============================================
) else (
    echo.
    echo ERREUR lors de la compilation.
)
echo.
pause

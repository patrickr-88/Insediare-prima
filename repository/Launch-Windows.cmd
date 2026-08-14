@echo off
REM USB Software Installer - Windows launcher.
REM
REM Double-click this file to start the graphical interface. Windows will ask
REM for administrator approval through the standard UAC prompt; that consent
REM dialog is the only privilege escalation the tool performs, and you will
REM always see it.
setlocal
set "REPO=%~dp0"
set "EXE=%REPO%bin\USBInstaller.exe"

if not exist "%EXE%" (
  echo Could not find %EXE%.
  echo Copy the Windows release build into the drive's bin\ folder.
  pause
  exit /b 2
)

net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Requesting administrator privileges...
  powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%EXE%' -ArgumentList '--repository','%REPO%'"
  exit /b 0
)

"%EXE%" --repository "%REPO%" %*

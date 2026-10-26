@echo off
title ClipMaker
cd /d "%~dp0"
if not exist "%~dp0setup.ps1" (
  echo.
  echo  ClipMaker needs to be unzipped first.
  echo  Close this window. In your Downloads folder, right-click the ClipMaker zip,
  echo  choose "Extract to", then open the new folder and double-click start.bat there.
  echo.
  pause
  exit /b 1
)
if not exist "runtime\ready.txt" (
  echo.
  echo  Setting up ClipMaker for the first time. This takes about 5-10 minutes.
  echo  You only have to wait for this once. Leave this window open.
  echo.
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
)
if not exist "runtime\ready.txt" (
  echo.
  echo  Setup hit a problem. Take a screenshot of this window and send it to Claude.
  pause
  exit /b 1
)
echo.
powershell -NoProfile -Command "try{(New-Object Net.Sockets.TcpClient).Connect('127.0.0.1',7860);exit 0}catch{exit 1}" >nul 2>nul && ( "runtime\python.exe" server.py --open & exit /b 0 )
echo  ClipMaker is running. Your browser will open in a moment.
echo  Keep this window open - closing it turns ClipMaker off.
echo.
:run
"runtime\python.exe" server.py
if "%errorlevel%"=="42" ( echo  Updated! Starting the new version... & goto run )
echo.
echo  ClipMaker stopped. If you see an error above, screenshot it and send it to Claude.
pause

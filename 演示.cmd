@echo off
chcp 65001 >nul
setlocal
start "" wscript.exe //B "%~dp0启动.vbs" --demo %*
exit /b 0

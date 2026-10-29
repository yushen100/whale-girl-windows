@echo off
chcp 65001 >nul 2>nul
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\1029_一键部署_v1.ps1" %*
set "CODE=%ERRORLEVEL%"
if not "%~1"=="" exit /b %CODE%
if not "%CODE%"=="0" (
  echo.
  echo 启动失败，请保留上方错误信息。
  pause
)
exit /b %CODE%

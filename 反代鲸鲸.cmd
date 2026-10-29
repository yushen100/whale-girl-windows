@echo off
chcp 65001 >nul 2>nul
cd /d "%~dp0"

rem 优先用 pwsh（PowerShell 7），没有就用系统自带的 powershell
set "PS=powershell"
where pwsh >nul 2>nul && set "PS=pwsh"

"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\whale-console.ps1" %*

set "taskExit=%errorlevel%"
rem 带参数调用（自动化用）就不暂停，直接返回退出码
if not "%~1"=="" exit /b %taskExit%
if not "%taskExit%"=="0" pause
exit /b %taskExit%

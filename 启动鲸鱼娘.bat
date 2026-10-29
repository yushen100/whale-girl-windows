@echo off
chcp 65001 >nul 2>nul
call "%~dp0一键部署鲸鲸.cmd" %*
exit /b %ERRORLEVEL%

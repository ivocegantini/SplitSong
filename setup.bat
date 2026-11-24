@echo off
rem Atalho para o setup.ps1 (permite rodar com dois cliques, sem mudar a ExecutionPolicy).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
set RC=%ERRORLEVEL%
if "%~1"=="" pause
exit /b %RC%

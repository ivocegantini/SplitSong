@echo off
rem Atalho para o build.ps1 (permite rodar com dois cliques, sem mudar a ExecutionPolicy).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1" %*
set RC=%ERRORLEVEL%
if "%~1"=="" pause
exit /b %RC%

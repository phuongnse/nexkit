@echo off
setlocal
python "%~dp0nexkit" %*
exit /b %errorlevel%

@echo off
REM Starts the bot in its own window; put a shortcut to this file in shell:startup so it starts at logon.
cd /d %~dp0\..
call .venv\Scripts\activate.bat
:loop
earnings-bot run
echo bot exited with %ERRORLEVEL%, restarting in 20s
timeout /t 20 /nobreak >nul
goto loop

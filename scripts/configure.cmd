@echo off
setlocal
py -3 "%~dp0..\src\wechat_publisher.py" configure --update
pause

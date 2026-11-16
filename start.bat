@echo off
cd /d "%~dp0"
chcp 65001 >nul
echo.
echo  ============================================
echo   Yingdao Community Auto Answer System
echo   http://127.0.0.1:8770
echo  ============================================
echo.
start "" "http://127.0.0.1:8770/"
python server.py 8770
echo.
echo Server stopped.
pause

@echo off
chcp 65001 >nul
title RVC 变声台
setlocal
set "ROOT=%~dp0.."
set "ROOT=%ROOT:~0,-1%"
rem RVC 引擎目录（含 assets/ configs/ 的那个 RVC-WebUI）
set "RVC_ROOT=%~dp0..\..\RVC-WebUI"
set "PYTHON=%RVC_ROOT%\..\runtime\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

cd /d "%ROOT%"
echo.
echo   ==========================================
echo    RVC 变声台
echo    正在启动本地服务，浏览器会自动打开
echo    关闭这个窗口即停止服务
echo   ==========================================
echo.
"%PYTHON%" -E -s -X utf8 -u "server\realtime_web.py"
if errorlevel 1 (
  echo.
  echo  启动失败，请检查上面的错误信息。按任意键关闭。
  pause >nul
)
endlocal

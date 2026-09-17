@echo off
rem ============================================================
rem  KeyPrism 一键启动 (Windows)
rem  用法:  start.cmd [音频文件] [后端端口] [前端端口]
rem  示例:  start.cmd demo.m4a
rem         start.cmd "D:\Music\song.mp3" 8800 5180
rem ============================================================
setlocal
cd /d "%~dp0"

set "AUDIO=%~1"
if "%AUDIO%"=="" set "AUDIO=demo.m4a"
set "BPORT=%~2"
if "%BPORT%"=="" set "BPORT=8800"
set "FPORT=%~3"
if "%FPORT%"=="" set "FPORT=5180"

if not exist "%AUDIO%" (
  echo [错误] 音频文件不存在: %AUDIO%
  echo 用法: start.cmd ^<音频文件^> [后端端口] [前端端口]
  pause
  exit /b 1
)

where uv >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 uv, 请先安装: https://docs.astral.sh/uv/
  pause
  exit /b 1
)

if not exist "frontend\node_modules" (
  echo [初始化] 安装前端依赖...
  pushd frontend
  call npm install --no-fund --no-audit
  popd
)

echo [启动] 后端 API  http://localhost:%BPORT%  (%AUDIO%)
start "KeyPrism Backend" cmd /k uv run python backend.py "%AUDIO%" --serve %BPORT%

echo [启动] 前端页面  http://localhost:%FPORT%
pushd frontend
start "KeyPrism Frontend" cmd /k npm run dev -- --port %FPORT% --strictPort
popd

echo.
echo 浏览器打开: http://localhost:%FPORT%
echo (两个窗口分别关闭即停止前后端)
timeout /t 3 >nul
start http://localhost:%FPORT%

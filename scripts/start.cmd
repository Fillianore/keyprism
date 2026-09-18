@echo off
rem ============================================================
rem  KeyPrism 一键启动 (Windows)
rem  用法:  scripts\start.cmd [音频文件]    缺省由后端默认加载 assets/demo.m4a
rem
rem  配置优先级: 环境变量 > %KEYPRISM_HOME%\config.env > 内置默认值
rem    KEYPRISM_HOME            工作区目录 (日志/缓存/上传暂存)
rem    KEYPRISM_LOG_DIR         日志目录   (默认 %KEYPRISM_HOME%\logs)
rem    KEYPRISM_API_HOST/PORT   后端 API 监听地址
rem    KEYPRISM_FRONTEND_PORT   前端页面端口
rem  config.env 为 KEY=VALUE 纯文本 (# 开头为注释)。
rem  在 VS Code 集成终端运行时, 两个端口会自动出现在 Ports 面板。
rem ============================================================
setlocal EnableDelayedExpansion
for %%I in ("%~dp0..") do set "PROJ_ROOT=%%~fI"
cd /d "%PROJ_ROOT%"

rem ---- 工作区目录: 日志 / 缓存 / 上传暂存 (KEYPRISM_HOME 可重定向) ----
if not defined KEYPRISM_HOME set "KEYPRISM_HOME=%USERPROFILE%\.keyprism"
if "!KEYPRISM_HOME:~0,1!"=="~" set "KEYPRISM_HOME=%USERPROFILE%!KEYPRISM_HOME:~1!"
if not exist "!KEYPRISM_HOME!" mkdir "!KEYPRISM_HOME!"

rem 用户持久配置: 存在则加载 (已设置的环境变量优先, 不被覆盖)
if exist "!KEYPRISM_HOME!\config.env" (
    for /f "usebackq eol=# tokens=1* delims==" %%K in ("!KEYPRISM_HOME!\config.env") do (
        set "CHK=%%K"
        if /i "!CHK:~0,9!"=="KEYPRISM_" if not defined %%K set "%%K=%%V"
    )
)

if not defined KEYPRISM_LOG_DIR set "KEYPRISM_LOG_DIR=!KEYPRISM_HOME!\logs"
if not exist "!KEYPRISM_LOG_DIR!" mkdir "!KEYPRISM_LOG_DIR!"
if not exist "!KEYPRISM_HOME!\cache\matplotlib" mkdir "!KEYPRISM_HOME!\cache\matplotlib"

rem ---- 监听地址与端口 (不沿用旧版 8800/5180) ----
if not defined KEYPRISM_API_HOST set "KEYPRISM_API_HOST=127.0.0.1"
if not defined KEYPRISM_API_PORT set "KEYPRISM_API_PORT=9630"
if not defined KEYPRISM_FRONTEND_PORT set "KEYPRISM_FRONTEND_PORT=5270"

set "MPLCONFIGDIR=!KEYPRISM_HOME!\cache\matplotlib"
set "PYTHONUNBUFFERED=1"

rem ---- 预检 ----
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

echo Starting backend API (port !KEYPRISM_API_PORT!)...
if not "%~1"=="" (
    start "KeyPrism API" /min cmd /c ""uv" run python -m keyprism "%~1" --serve !KEYPRISM_API_PORT! --host !KEYPRISM_API_HOST! > "!KEYPRISM_LOG_DIR!\backend.log" 2>&1"
) else (
    start "KeyPrism API" /min cmd /c ""uv" run python -m keyprism --serve !KEYPRISM_API_PORT! --host !KEYPRISM_API_HOST! > "!KEYPRISM_LOG_DIR!\backend.log" 2>&1"
)

rem 等待 API 就绪 (首次 uv sync + 频谱分析较慢; 超时仅提示)
powershell -NoProfile -Command "$u='http://127.0.0.1:!KEYPRISM_API_PORT!/api/ping'; for($i=0;$i -lt 120;$i++){ try{ (New-Object Net.WebClient).DownloadString($u) | Out-Null; exit 0 }catch{ Start-Sleep -Seconds 1 } }; exit 1" >nul 2>nul
if errorlevel 1 echo [警告] API 120s 内未就绪, 详见 "!KEYPRISM_LOG_DIR!\backend.log"

echo Starting Vite frontend (port !KEYPRISM_FRONTEND_PORT!)...
start "KeyPrism Frontend" /min cmd /c "cd /d "%PROJ_ROOT%\frontend" && npm run dev -- --port !KEYPRISM_FRONTEND_PORT! --strictPort > "!KEYPRISM_LOG_DIR!\vite.log" 2>&1"

echo.
echo All services running:
echo   Backend API : http://!KEYPRISM_API_HOST!:!KEYPRISM_API_PORT!
echo   Frontend    : http://localhost:!KEYPRISM_FRONTEND_PORT!
echo.
echo Workspace: !KEYPRISM_HOME!
echo Logs: "!KEYPRISM_LOG_DIR!\backend.log" / "vite.log"
echo (关闭两个最小化的 KeyPrism 窗口即停止对应服务)

timeout /t 3 >nul
start http://localhost:!KEYPRISM_FRONTEND_PORT!
endlocal

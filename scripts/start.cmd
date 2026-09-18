@echo off
rem ============================================================
rem  KeyPrism one-command launch (Windows)
rem  Usage:  scripts\start.cmd [audio file]   the backend loads assets/demo.m4a by default
rem
rem  Config precedence: env vars > %KEYPRISM_HOME%\config.env > built-in defaults
rem    KEYPRISM_HOME            workspace directory (logs/cache/upload staging)
rem    KEYPRISM_LOG_DIR         log directory (default %KEYPRISM_HOME%\logs)
rem    KEYPRISM_API_HOST/PORT   backend API listen address
rem    KEYPRISM_FRONTEND_PORT   frontend page port
rem  config.env is plain KEY=VALUE text (lines starting with # are comments).
rem  In the VS Code integrated terminal, both ports appear in the Ports panel.
rem ============================================================
setlocal EnableDelayedExpansion
for %%I in ("%~dp0..") do set "PROJ_ROOT=%%~fI"
cd /d "%PROJ_ROOT%"

rem ---- Workspace: logs / cache / upload staging (relocatable via KEYPRISM_HOME) ----
if not defined KEYPRISM_HOME set "KEYPRISM_HOME=%USERPROFILE%\.keyprism"
if "!KEYPRISM_HOME:~0,1!"=="~" set "KEYPRISM_HOME=%USERPROFILE%!KEYPRISM_HOME:~1!"
if not exist "!KEYPRISM_HOME!" mkdir "!KEYPRISM_HOME!"

rem User persistent config: loaded if present (already-set env vars win)
if exist "!KEYPRISM_HOME!\config.env" (
    for /f "usebackq eol=# tokens=1* delims==" %%K in ("!KEYPRISM_HOME!\config.env") do (
        set "CHK=%%K"
        if /i "!CHK:~0,9!"=="KEYPRISM_" if not defined %%K set "%%K=%%V"
    )
)

if not defined KEYPRISM_LOG_DIR set "KEYPRISM_LOG_DIR=!KEYPRISM_HOME!\logs"
if not exist "!KEYPRISM_LOG_DIR!" mkdir "!KEYPRISM_LOG_DIR!"
if not exist "!KEYPRISM_HOME!\cache\matplotlib" mkdir "!KEYPRISM_HOME!\cache\matplotlib"

rem ---- Listen address and ports (old 8800/5180 intentionally not reused) ----
if not defined KEYPRISM_API_HOST set "KEYPRISM_API_HOST=127.0.0.1"
if not defined KEYPRISM_API_PORT set "KEYPRISM_API_PORT=9630"
if not defined KEYPRISM_FRONTEND_PORT set "KEYPRISM_FRONTEND_PORT=5270"

set "MPLCONFIGDIR=!KEYPRISM_HOME!\cache\matplotlib"
set "PYTHONUNBUFFERED=1"

rem ---- Preflight ----
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

rem Wait for the API to be ready (first uv sync + spectrum analysis are slow; timeout only warns)
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

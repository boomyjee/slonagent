@echo off
setlocal

set CONFIG=%~dp0.config.json

:: Читаем значения из .config.json через PowerShell
for /f "delims=" %%i in ('powershell -NoProfile -Command "$c = Get-Content '%CONFIG%' | ConvertFrom-Json; $c.env.GEMINI_API_KEY"') do set GEMINI_API_KEY=%%i
for /f "delims=" %%i in ('powershell -NoProfile -Command "$c = Get-Content '%CONFIG%' | ConvertFrom-Json; $c.env.HTTP_PROXY"') do set PROXY=%%i

if "%GEMINI_API_KEY%"=="" (
    echo [ERROR] GEMINI_API_KEY not found in .config.json
    exit /b 1
)

echo [Hindsight] LLM:  gemini / gemini-2.5-flash
if not "%PROXY%"=="" echo [Hindsight] Proxy: %PROXY%
echo [Hindsight] Data: %USERPROFILE%\.hindsight
echo [Hindsight] API:  http://localhost:8888
echo [Hindsight] UI:   http://localhost:9999
echo.

if "%PROXY%"=="" (
    podman run --rm -d ^
        -p 8888:8888 -p 9999:9999 ^
        -e HINDSIGHT_API_LLM_PROVIDER=gemini ^
        -e HINDSIGHT_API_LLM_API_KEY=%GEMINI_API_KEY% ^
        -v "%USERPROFILE%\.hindsight:/home/hindsight/.pg0" ^
        ghcr.io/vectorize-io/hindsight:latest
) else (
    podman run --rm -d ^
        -p 8888:8888 -p 9999:9999 ^
        -e HINDSIGHT_API_LLM_PROVIDER=gemini ^
        -e HINDSIGHT_API_LLM_API_KEY=%GEMINI_API_KEY% ^
        -e HTTP_PROXY=%PROXY% ^
        -e HTTPS_PROXY=%PROXY% ^
        -e http_proxy=%PROXY% ^
        -e https_proxy=%PROXY% ^
        -v "%USERPROFILE%\.hindsight:/home/hindsight/.pg0" ^
        ghcr.io/vectorize-io/hindsight:latest
)

if %errorlevel% neq 0 (
    echo [ERROR] Failed to start. Is Podman running?
    exit /b 1
)

echo [Hindsight] Server started. Logs: podman logs -f ^<container_id^>
endlocal

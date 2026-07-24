@echo off
setlocal
set "GROK_WORKER_ROOT=%~dp0.."
if exist "%GROK_WORKER_ROOT%\.venv\Scripts\grok-worker-agent.exe" (
  "%GROK_WORKER_ROOT%\.venv\Scripts\grok-worker-agent.exe" %*
) else (
  uv run --project "%GROK_WORKER_ROOT%" grok-worker-agent %*
)
exit /b %ERRORLEVEL%

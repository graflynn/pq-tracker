@echo off
REM Launcher for pq_tracker.mcp_server. Claude Code / Cowork spawn this as
REM the stdio MCP process.
REM
REM IMPORTANT: the plugin is installed to a directory separate from the
REM source repo (e.g. Cowork copies it into ...\rpm\plugin_<hash>\), so we
REM CANNOT use %~dp0\.. to find the venv. The repo with .venv\ and the
REM pq_tracker\ Python package lives at a fixed local path — set below.
REM
REM If you move the repo, edit PQ_TRACKER_HOME (or set it as an env var in
REM the plugin's .mcp.json to override without touching this file).

if not defined PQ_TRACKER_HOME set "PQ_TRACKER_HOME=C:\Users\Grainne\Documents\pq-tracker"

if not exist "%PQ_TRACKER_HOME%\.venv\Scripts\python.exe" (
    echo [run-mcp] ERROR: cannot find venv python at "%PQ_TRACKER_HOME%\.venv\Scripts\python.exe" 1>&2
    echo [run-mcp] Edit run-mcp.cmd or set PQ_TRACKER_HOME to the pq-tracker repo root. 1>&2
    exit /b 1
)

cd /d "%PQ_TRACKER_HOME%"
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" -m pq_tracker.mcp_server

@echo off
REM Launcher for pq_tracker.mcp_server. Claude Code spawns this as the
REM stdio MCP process. We move into the repo root (parent of the plugin
REM folder) so `-m pq_tracker.mcp_server` resolves, and use the venv's
REM Python so all deps are available without needing a system Python on PATH.

cd /d "%~dp0\.."
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" -m pq_tracker.mcp_server

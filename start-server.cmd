@echo off
REM Start the pq-tracker UI + agent API on 127.0.0.1:5454.
REM
REM Endpoints:
REM   http://127.0.0.1:5454/          browser UI
REM   http://127.0.0.1:5454/api/v1/   agent JSON API (facets, pqs, pq/<ref>,
REM                                   aggregate, semantic, hse_pdfs, sql)
REM
REM To require Bearer-token auth on the API endpoints, uncomment the SET line
REM below and pick any value. Without it, the API is open on localhost.
REM
REM   set PQ_API_TOKEN=replace-me-with-a-secret

cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" -m pq_tracker.ui --host 127.0.0.1 --port 5454 --no-open

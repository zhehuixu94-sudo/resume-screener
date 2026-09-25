@echo off
REM ============================================================
REM  Resume Screener - one-click runner (Windows)
REM  Added by Hermes (PROJECT-ALIGN-20260926, "complete project" gap #3)
REM  Idempotent: every step can be re-run alone. Fails fast with a reason.
REM  Usage: run.bat          full flow (db -> env -> initdb -> check)
REM         run.bat check    self-check only (pytest + contract validation)
REM         run.bat db       start/ create the database container
REM         run.bat env      create venv and install requirements
REM         run.bat initdb   create tables / migrate
REM  NOTE: ASCII only - cmd.exe reads .bat in the OEM codepage, so UTF-8
REM        (Chinese) comments break parsing. Keep this file ASCII.
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "CONTAINER=resume_pg"
set "IMAGE=pgvector/pgvector:pg18"
set "DBNAME=resume_screener"
set "DBUSER=resume_app"
set "PGPORT_HOST=5433"
set "VENV_PY=.venv\Scripts\python.exe"
if not defined PGHOST     set "PGHOST=127.0.0.1"
if not defined PGPORT     set "PGPORT=%PGPORT_HOST%"
if not defined PGUSER     set "PGUSER=%DBUSER%"
if not defined PGDATABASE set "PGDATABASE=%DBNAME%"

set "STEP=%~1"
if "%STEP%"=="" set "STEP=all"

if /i "%STEP%"=="all"    goto db
if /i "%STEP%"=="check"  goto check
if /i "%STEP%"=="db"     goto db
if /i "%STEP%"=="env"    goto env
if /i "%STEP%"=="initdb" goto initdb
echo [ERROR] unknown argument "%STEP%" - use: check ^| db ^| env ^| initdb
exit /b 2

:db
echo.
echo [1/4] database container %CONTAINER%
docker version >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Docker is not available. Start Docker Desktop and re-run.
  exit /b 1
)
REM Container detection uses `docker inspect`, NOT `findstr` on `docker ps` output:
REM docker.exe prints LF-only lines, and findstr /x cannot match LF-only input
REM (verified 2026-09-25: same name, `findstr /x` on the piped output returns 1).
REM That made this script non-idempotent - every re-run hit "Conflict. The
REM container name ... is already in use" and exited 1.
set "RUNNING="
for /f "delims=" %%i in ('docker inspect -f "{{.State.Running}}" %CONTAINER% 2^>nul') do set "RUNNING=%%i"
if /i "!RUNNING!"=="true" (
  echo        already running.
  goto db_done
)
if /i "!RUNNING!"=="false" (
  echo        exists but stopped - starting...
  docker start "%CONTAINER%" >nul
  if errorlevel 1 (echo [ERROR] docker start failed & exit /b 1)
  goto db_done
)
if "%PGPASSWORD%"=="" (
  echo [ERROR] database password required: set PGPASSWORD=... then re-run
  echo         ^(password is never stored in this script or in the repo^)
  exit /b 1
)
echo        creating container ^(host port %PGPORT_HOST% -^> 5432^)...
docker run -d --name "%CONTAINER%" -e POSTGRES_DB=%DBNAME% -e POSTGRES_USER=%DBUSER% -e POSTGRES_PASSWORD=%PGPASSWORD% -p %PGPORT_HOST%:5432 -v resume_pg_data:/var/lib/postgresql/data "%IMAGE%" >nul
if errorlevel 1 (echo [ERROR] docker run failed & exit /b 1)

:db_done
if /i "%STEP%"=="db" goto done

:env
echo.
echo [2/4] python environment .venv
if not exist "%VENV_PY%" (
  echo        creating venv...
  python -m venv .venv
  if errorlevel 1 (echo [ERROR] venv creation failed ^(need python 3.11^) & exit /b 1)
)
"%VENV_PY%" -m pip install -q --upgrade pip
"%VENV_PY%" -m pip install -q -r requirements-dev.txt
if errorlevel 1 (echo [ERROR] dependency install failed & exit /b 1)
"%VENV_PY%" -V

:env_done
if /i "%STEP%"=="env" goto done

:initdb
echo.
echo [3/4] create tables / migrate ^(idempotent^)
"%VENV_PY%" tools\init_db.py --show
if errorlevel 1 (echo [ERROR] init_db failed - is the container up and DSN correct? & exit /b 1)

:initdb_done
if /i "%STEP%"=="initdb" goto done

:check
echo.
echo [4/4] offline self-check ^(no token spent^)
"%VENV_PY%" -m pytest tests -q
if errorlevel 1 (echo [ERROR] tests not green & exit /b 1)
"%VENV_PY%" tools\validate_contracts.py --no-live
if errorlevel 1 (echo [ERROR] contract validation failed & exit /b 1)

echo.
echo ============================================================
echo  READY. Daily use:
echo    enqueue: .venv\Scripts\python -m app.batch enqueue --job config\job_hr_admin.json --resumes "RESUME_DIR\"
echo    run:     .venv\Scripts\python -m app.batch run     --job config\job_hr_admin.json --workers 3
echo    report:  .venv\Scripts\python -m app.batch report  --job config\job_hr_admin.json --outdir out\reports
echo  Reminder: the database container does not auto-start - after a reboot run: run.bat db
echo ============================================================

:done
endlocal

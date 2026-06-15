@echo off
REM Runs the follow-up engine once. Scheduled daily by Windows Task Scheduler.
REM Detects replies, then sends any due follow-ups to non-repliers.
cd /d "%~dp0"
python followups.py >> followups_run.log 2>&1

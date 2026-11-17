@echo off
rem Double-click to start Swifteye and open it in the browser.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
if errorlevel 1 pause

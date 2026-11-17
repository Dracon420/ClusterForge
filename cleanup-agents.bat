@echo off
REM One-time cleanup of stray ClusterForge agent processes.
REM Right-click > Run as administrator. Does not touch the ARK servers.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\cleanup-agents.ps1"
pause

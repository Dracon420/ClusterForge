@echo off
REM Allow the ClusterForge web UI through Windows Firewall on this PC (LAN only).
REM Right-click > Run as administrator.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\allow-panel-firewall.ps1"
pause

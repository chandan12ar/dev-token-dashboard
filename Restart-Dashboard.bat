@echo off
rem Double-click this to restart the Dev Token Dashboard with the latest code.
title Restart Dev Token Dashboard
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Restart-Dashboard.ps1"

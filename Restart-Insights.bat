@echo off
rem Double-click this to restart AI Fluency Insights with the latest code.
title Restart AI Fluency Insights
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Restart-Insights.ps1"

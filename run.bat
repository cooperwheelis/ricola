@echo off
title ricola
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Missing .venv. From this folder run:
  echo   py -3.13 -m venv .venv
  echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
  pause
  exit /b 1
)
.venv\Scripts\python.exe ricola.py
if errorlevel 1 pause

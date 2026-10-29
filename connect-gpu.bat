@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 远程 GPU 隧道（保持打开）
where ssh >nul 2>nul
if errorlevel 1 (
  echo [错误] 没有找到 ssh。请在「设置 - 应用 - 可选功能」里添加「OpenSSH 客户端」
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\connect_gpu.ps1"
pause

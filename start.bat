@echo off
chcp 65001 >nul
cd /d "%~dp0"
title B站转录

where uv >nul 2>nul
if errorlevel 1 (
  echo [错误] 没有找到 uv，请先安装：
  echo   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  pause
  exit /b 1
)

echo 正在检查依赖（首次运行要安装依赖，第一次识别时会再下载约 2 GB 模型）...
uv sync
if errorlevel 1 (
  echo [错误] 依赖安装失败
  pause
  exit /b 1
)

uv run python -m app.main %*
pause

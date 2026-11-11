@echo off
chcp 65001 >nul
title RNA Companion - 下载本地模型
echo == RNA Companion 首次安装：下载记忆检索所需的两个本地模型（约 3.3 GB）==
echo.

cd /d "%~dp0backend"

if not exist .venv\Scripts\python.exe (
    echo 请先运行 安装依赖.bat
    pause
    exit /b 1
)

.venv\Scripts\python.exe scripts\download_models.py
if errorlevel 1 ( echo 模型下载失败，请检查网络后重新运行本脚本（支持断点续传） & pause & exit /b 1 )

echo.
echo 模型下载完成。下一步：
echo   1. 复制 .env.example 为 .env，填入 RNA_REPLY_API_KEY 和 DEFAULT_API_KEY
echo   2. 双击 RNA Companion.exe 启动应用
pause

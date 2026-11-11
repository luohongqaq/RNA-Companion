@echo off
chcp 65001 >nul
title RNA Companion - 安装依赖
echo == RNA Companion 首次安装：创建虚拟环境并安装后端依赖 ==
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo 未检测到 Python。请先安装 Python 3.12，安装时勾选 "Add python.exe to PATH"。
    echo 下载地址: https://www.python.org/downloads/release/python-31210/
    pause
    exit /b 1
)
python --version

cd /d "%~dp0backend"

if not exist .venv (
    echo 创建虚拟环境 backend\.venv ...
    python -m venv .venv
    if errorlevel 1 ( echo 虚拟环境创建失败 & pause & exit /b 1 )
)

echo 安装后端依赖（几分钟）...
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\pip install -r requirements.txt
if errorlevel 1 ( echo 依赖安装失败，请检查网络后重新运行本脚本 & pause & exit /b 1 )

echo.
echo 依赖安装完成。下一步：双击 下载模型.bat
pause

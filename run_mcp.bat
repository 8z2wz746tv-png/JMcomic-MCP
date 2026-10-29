@echo off
REM 启动 JMComic MCP server（stdio）—— Windows 版。
REM
REM 为什么需要这个：run_mcp.sh 是 bash 脚本，Windows 宿主（WorkBuddy /
REM Cherry Studio / Claude Desktop）根本执行不了 .sh，配上去只会一直显示
REM "正在连接"然后断开。
REM
REM 注意：stdout 是 JSON-RPC 协议通道，这里除了 python 的输出不许有任何杂音，
REM 所以所有提示一律打到 stderr（1>&2）。
setlocal

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

set "VENV_PY=%SCRIPT_DIR%\venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo [jmcomic-mcp] 找不到虚拟环境解释器: %VENV_PY% 1>&2
    echo [jmcomic-mcp] 请先执行: python -m venv venv ^&^& venv\Scripts\pip install -e . ^&^& venv\Scripts\pip install -r requirements-mcp.txt 1>&2
    exit /b 1
)

"%VENV_PY%" -c "import mcp" 1>nul 2>nul
if errorlevel 1 (
    echo [jmcomic-mcp] 缺少 mcp 依赖，请执行: 1>&2
    echo     "%VENV_PY%" -m pip install -r "%SCRIPT_DIR%\requirements-mcp.txt" 1>&2
    exit /b 1
)

set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONIOENCODING=utf-8"

"%VENV_PY%" "%SCRIPT_DIR%\jmcomic_mcp\server.py"
exit /b %errorlevel%

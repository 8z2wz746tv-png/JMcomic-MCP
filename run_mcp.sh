#!/bin/bash
# 启动 JMComic MCP server（stdio）。
# MCP 宿主会把这个脚本当子进程拉起来，通过 stdin/stdout 说 JSON-RPC。
#
# 注意：这里的提示信息全部写 stderr —— stdout 是协议通道，不许有任何杂音。

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="$SCRIPT_DIR/venv/bin/python"

if [ ! -x "$VENV_PY" ]; then
    echo "❌ 找不到虚拟环境解释器: $VENV_PY" >&2
    echo "   请先创建并安装依赖：python3 -m venv venv && ./venv/bin/pip install -e ." >&2
    exit 1
fi

if ! "$VENV_PY" -c "import mcp" 2>/dev/null; then
    echo "❌ 缺少 mcp 依赖，请执行：" >&2
    echo "   $VENV_PY -m pip install -r $SCRIPT_DIR/requirements-mcp.txt" >&2
    exit 1
fi

# 不写 .pyc，免得 server 每次被拉起都往项目目录里撒 __pycache__
export PYTHONDONTWRITEBYTECODE=1

exec "$VENV_PY" "$SCRIPT_DIR/jmcomic_mcp/server.py"

# -*- coding: utf-8 -*-
"""jmcomic 的 MCP server 实现（stdio）。"""

from .tasks import (  # noqa: F401
    DEFAULT_DEST_DIR,
    DownloadTask,
    JmcomicMcpError,
    TaskManager,
    build_option,
    parse_targets,
    resolve_dest_dir,
)

__all__ = [
    "DEFAULT_DEST_DIR",
    "DownloadTask",
    "JmcomicMcpError",
    "TaskManager",
    "build_option",
    "parse_targets",
    "resolve_dest_dir",
]

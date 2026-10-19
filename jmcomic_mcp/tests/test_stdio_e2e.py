# -*- coding: utf-8 -*-
"""
stdio 端到端自测：真的把 server.py 当子进程拉起来，走 MCP 协议对话。

这里覆盖两件最容易翻车、又最难自查的事：
1. stdout 必须只有 JSON-RPC 协议帧 —— jmcomic 默认用 print() 打日志，
   如果漏到 stdout，宿主连接会直接断。
2. 9 个工具能被正确注册、参数校验和确认门在协议层同样生效。

不联网（只调用不触网的工具）。
跑法： ./venv/bin/python -m unittest jmcomic_mcp.tests.test_stdio_e2e -v
"""
import asyncio
import os
import subprocess
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from mcp import Client, StdioServerParameters  # noqa: E402

EXPECTED_TOOLS = {
    "search_album", "get_album_detail", "get_photo_detail", "get_ranking",
    "get_favorites", "start_download", "get_task", "list_tasks", "cancel_task",
}


def _params():
    return StdioServerParameters(
        command=sys.executable,
        args=[os.path.join(_ROOT, "jmcomic_mcp", "server.py")],
        cwd=_ROOT,
    )


class TestStdioProtocol(unittest.TestCase):

    def test_list_tools_over_protocol(self):
        async def run():
            async with Client(_params()) as client:
                return await client.list_tools()

        result = asyncio.run(run())
        names = {t.name for t in result.tools}
        self.assertEqual(names, EXPECTED_TOOLS)

    def test_confirmation_gate_over_protocol(self):
        async def run():
            async with Client(_params()) as client:
                not_confirmed = await client.call_tool(
                    "start_download", {"album_ids": "422866"})
                bad_dir = await client.call_tool(
                    "start_download", {"album_ids": "422866",
                                       "dest_dir": "relative/x", "confirmed": True})
                missing = await client.call_tool("get_task", {"task_id": "nope"})
                return not_confirmed, bad_dir, missing

        not_confirmed, bad_dir, missing = asyncio.run(run())

        # mcp v2 把返回值包在 structured_content['result'] 里
        plan = not_confirmed.structured_content["result"]
        self.assertEqual(plan["status"], "confirmation_required")
        self.assertNotIn("task_id", plan)

        self.assertIn("error", bad_dir.structured_content["result"])
        self.assertIn("error", missing.structured_content["result"])


class TestStdoutPurity(unittest.TestCase):
    """jmcomic 的日志执行器默认是 print → stdout；server 启动时必须把它掰到 stderr。"""

    def test_no_stdout_pollution(self):
        code = (
            "import sys; sys.path.insert(0, '.');"
            "import jmcomic_mcp.server;"          # 导入即完成 stdout 保护
            "from jmcomic.jm_config import JmModuleConfig;"
            "JmModuleConfig.jm_log('selftest', '\u8fd9\u884c\u5fc5\u987b\u53bb stderr');"
            "print('\u8fd9\u884c print \u4e5f\u5fc5\u987b\u53bb stderr')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=_ROOT, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "", f"stdout 被污染了: {proc.stdout!r}")
        self.assertIn("必须去 stderr", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)

# -*- coding: utf-8 -*-
"""
联网自检：确认网络/域名链路可用，并且下载路径真的落在指定的 dest_dir 下。

**这个脚本不会下载任何图片**：它用替身替换掉"下载图片"这一步，只解析保存路径，
所以可以放心跑。会联网拉取本子/章节的元数据。

跑法：
    ./venv/bin/python jmcomic_mcp/tests/live_check.py [album_id]
"""
import os
import shutil
import sys
import tempfile
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_ROOT, os.path.join(_ROOT, "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from jmcomic_mcp import tasks as T  # noqa: E402
from jmcomic_mcp import server as S  # noqa: E402

ALBUM_ID = sys.argv[1] if len(sys.argv) > 1 else "422866"


def log(msg):
    sys.stderr.write(f"{msg}\n")
    sys.stderr.flush()


def check_read_tools():
    log("\n[1/3] 只读工具（搜索 / 详情）")

    result = S.search_album(query="オリジナル", limit=3)
    if "error" in result:
        log(f"  ✗ search_album: {result['error']}")
        return False
    log(f"  ✓ search_album 返回 {result['count']} 条（total={result['total']}）")

    detail = S.get_album_detail(album_id=ALBUM_ID, limit=5)
    if "error" in detail:
        log(f"  ✗ get_album_detail: {detail['error']}")
        return False
    log(f"  ✓ get_album_detail: 章节数={detail['chapter_count']}, "
        f"总页数={detail['page_count']}, 返回章节前 {len(detail['chapters'])} 条")

    ranking = S.get_ranking(period="day", limit=3)
    if "error" in ranking:
        log(f"  ✗ get_ranking: {ranking['error']}")
        return False
    log(f"  ✓ get_ranking 日榜返回 {ranking['count']} 条")

    return True


class _DryRunDownloader(T._CancellableDownloader):
    """只解析保存路径（顺带建目录），不真的下载图片。"""

    images = []

    def download_by_image_detail(self, image):
        if self.task.cancel_event.is_set():
            return
        image.save_path = self.option.decide_image_filepath(image)
        _DryRunDownloader.images.append(image.save_path)
        self.task.count_image_done()


def check_dest_dir_end_to_end():
    log("\n[2/3] 下载路径端到端（dry run，不下载图片）")

    dest = tempfile.mkdtemp(prefix="jmcomic-mcp-live-")
    real = T._CancellableDownloader
    T._CancellableDownloader = _DryRunDownloader
    try:
        manager = T.TaskManager()
        task = manager.submit([ALBUM_ID], [], dest_dir=dest)
        for _ in range(600):
            if task.snapshot()["is_finished"]:
                break
            time.sleep(0.2)
        snap = task.snapshot()
    finally:
        T._CancellableDownloader = real

    log(f"  任务状态: {snap['state']}, 进度: {snap['progress_percent']}%, "
        f"图片: {snap['images']}, 章节: {snap['photos']}")
    if snap["error"]:
        log(f"  ✗ 错误: {snap['error']}")
        shutil.rmtree(dest, ignore_errors=True)
        return False

    paths = _DryRunDownloader.images
    if not paths:
        log("  ✗ 没有解析出任何保存路径")
        shutil.rmtree(dest, ignore_errors=True)
        return False

    outside = [p for p in paths if not p.startswith(dest + os.sep)]
    log(f"  ✓ 解析出 {len(paths)} 个保存路径，全部位于 dest_dir 下: {not outside}")
    log(f"    样例: {paths[0].replace(dest, '<dest>')}")
    if outside:
        log(f"  ✗ 有 {len(outside)} 个路径跑到 dest_dir 外面，样例: {outside[0]}")
        shutil.rmtree(dest, ignore_errors=True)
        return False

    shutil.rmtree(dest, ignore_errors=True)
    return True


def check_confirmation_gate():
    log("\n[3/3] 写入确认门")
    plan = S.start_download(album_ids=ALBUM_ID)
    ok = plan.get("status") == "confirmation_required" and "task_id" not in plan
    log(f"  {'✓' if ok else '✗'} 未确认时不启动下载，返回 status={plan.get('status')}")
    return ok


def main():
    log(f"JMComic MCP 联网自检（album={ALBUM_ID}）")
    results = [check_read_tools(), check_dest_dir_end_to_end(), check_confirmation_gate()]

    log("\n" + "=" * 46)
    if all(results):
        log("全部通过 ✓ 链路可用，可以接到 MCP 宿主了。")
        return 0
    log("有检查未通过 ✗ 见上面输出。")
    return 1


if __name__ == "__main__":
    sys.exit(main())

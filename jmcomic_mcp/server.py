#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JMComic MCP server（stdio）。

把 jmcomic 的搜索 / 详情 / 排行 / 收藏夹，以及下载能力暴露成 MCP 工具。

两条硬约束
----------
1. **stdio 模式下 stdout 是 JSON-RPC 协议通道**。jmcomic 自带的日志走的是
   `print()`（见 jm_config.default_jm_logging），第三方库里也有不少 `print`，
   任何一行漏到 stdout 都会破坏协议帧、把宿主连接搞断。所以本文件在导入任何
   第三方模块之前就把 `builtins.print` 重定向到 stderr，并把 jmcomic 的日志
   执行器换成 logging→stderr。**本文件及本包内永远不要用 print() 做诊断。**
2. 下载是长任务，工具调用不能阻塞：`start_download` 立刻返回 task_id，
   进度用 `get_task` / `list_tasks` 轮询，`cancel_task` 秒级生效。

运行：python jmcomic_mcp/server.py   （宿主会以 stdio 方式拉起它）
"""
from __future__ import annotations

import builtins
import logging
import os
import sys
import threading
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# 0. stdio 保护 —— 必须在导入 jmcomic / common 之前完成
# ---------------------------------------------------------------------------
_REAL_PRINT = builtins.print


def _print_to_stderr(*args, **kwargs):
    """把进程内所有 print 赶到 stderr，避免污染 stdio 协议通道。"""
    kwargs.setdefault("file", sys.stderr)
    return _REAL_PRINT(*args, **kwargs)


if builtins.print is not _print_to_stderr:
    builtins.print = _print_to_stderr

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="[jmcomic-mcp] %(asctime)s %(levelname)s: %(message)s",
)
logger = logging.getLogger("jmcomic_mcp")

# ---------------------------------------------------------------------------
# 1. 让 jmcomic 可导入
#     正常安装（pip install -e .）时不需要，直接跑脚本时兜底。
# ---------------------------------------------------------------------------
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_ROOT, os.path.join(_ROOT, "src")):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

from mcp.server import MCPServer  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

from jmcomic.jm_config import JmModuleConfig  # noqa: E402

from jmcomic_mcp.tasks import (  # noqa: E402
    DEFAULT_DEST_DIR,
    JmcomicMcpError,
    TaskManager,
    build_option,
    parse_targets,
    resolve_dest_dir,
)

# ---------------------------------------------------------------------------
# 2. 把 jmcomic 的日志执行器换成 stderr（默认实现是 print → stdout）
# ---------------------------------------------------------------------------
def _stderr_jm_log(topic: str, msg: str) -> None:
    logging.getLogger("jmcomic").info("【%s】%s", topic, msg)


JmModuleConfig.EXECUTOR_LOG = _stderr_jm_log

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)

ORDER_BY_HELP = "排序：mr=最新(默认) / mv=观看数 / mp=图片数 / tf=点赞数"
CATEGORY_HELP = ("分类：0=全部(默认) / doujin=同人 / single=单本 / short=短篇 / "
                 "hanman=韩漫 / meiman=美漫 / another=其他 / doujin_cosplay / 3D / english_site")

MAX_ITEMS = 50

mcp = MCPServer("jmcomic")
manager = TaskManager()

# ---------------------------------------------------------------------------
# 3. 只读工具
# ---------------------------------------------------------------------------

_client_cache: Dict[str, Any] = {}
_client_lock = threading.Lock()


def _client(config_file: str = ""):
    """按 config_file 缓存 JmcomicClient（首次创建会去拉取最新域名）。"""
    key = os.path.expanduser(config_file.strip()) if config_file else ""
    with _client_lock:
        client = _client_cache.get(key)
        if client is None:
            if key and not os.path.isfile(key):
                raise JmcomicMcpError(f"配置文件不存在: {key}")
            option = build_option(DEFAULT_DEST_DIR, config_file=key or None)
            client = option.new_jm_client()
            _client_cache[key] = client
        return client


def _page_items(page, limit: int) -> Dict[str, Any]:
    """把 JmPageContent 转成精简结构，避免把原始大 dict 全塞给模型。"""
    items = [
        {"album_id": aid, "title": name, "tags": tags}
        for aid, name, tags in page.iter_id_title_tag()
    ]
    limit = max(1, min(int(limit or 20), MAX_ITEMS))
    return {
        "total": getattr(page, "total", None),
        "page_size": page.page_size,
        "count": min(len(items), limit),
        "items": items[:limit],
        "truncated": len(items) > limit,
    }


@mcp.tool(annotations=READ_ONLY)
def search_album(query: str,
                 page: int = 1,
                 order_by: str = "mr",
                 time_range: str = "a",
                 category: str = "0",
                 sub_category: str = "",
                 limit: int = 20,
                 config_file: str = "",
                 ) -> Dict[str, Any]:
    """站内搜索本子，返回一批候选（album_id / 标题 / 标签）。

    这是「先查再决定下什么」的入口：拿到 album_id 后再调用 get_album_detail
    看章节，最后用 start_download 下载。

    :param query: 搜索关键词（作品名、作者名、社团名等）
    :param page: 页码，从 1 开始
    :param order_by: mr=最新(默认) / mv=观看数 / mp=图片数 / tf=点赞数
    :param time_range: 时间范围：a=全部(默认) / t=今天 / w=本周 / m=本月
    :param category: 分类：0=全部(默认) / doujin=同人 / single=单本 / short=短篇 /
                     hanman=韩漫 / meiman=美漫 / another=其他
    :param sub_category: 副分类（仅网页端支持），如 chinese=汉化 / japanese=日语
    :param limit: 最多返回多少条（1-50，默认 20），防止一次塞爆上下文
    :param config_file: 可选的 option.yml 绝对路径（配代理/自定义域名/登录态时用）
    """
    try:
        page_obj = _client(config_file).search_site(
            search_query=query,
            page=max(1, int(page)),
            order_by=order_by or "mr",
            time=time_range or "a",
            category=category or "0",
            sub_category=sub_category or None,
        )
    except JmcomicMcpError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.warning("search_album 失败: %s", e)
        return {"error": f"搜索失败: {type(e).__name__}: {e}"}

    result = _page_items(page_obj, limit)
    result.update({"query": query, "page": page})
    return result


@mcp.tool(annotations=READ_ONLY)
def get_album_detail(album_id: str,
                     limit: int = 200,
                     config_file: str = "",
                     ) -> Dict[str, Any]:
    """查本子详情：标题、作者、标签、总页数、章节列表（含每个章节的 photo_id）。

    下载前先看这个可以确认章节数和总页数，避免下到大部头。

    :param album_id: 本子 id，如 '422866' 或 'JM422866'
    :param limit: 章节列表最多返回多少条（默认 200）
    :param config_file: 可选的 option.yml 绝对路径
    """
    try:
        album = _client(config_file).get_album_detail(album_id)
    except JmcomicMcpError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.warning("get_album_detail 失败: %s", e)
        return {"error": f"获取本子详情失败: {type(e).__name__}: {e}"}

    limit = max(1, int(limit or 200))
    chapters = [
        {"photo_id": str(pid), "index": idx, "title": title}
        for pid, idx, title, *_ in album.episode_list
    ]

    return {
        "album_id": album.album_id,
        "title": album.title,
        "authors": album.authors,
        "tags": album.tags,
        "works": album.works,
        "actors": album.actors,
        "page_count": album.page_count,
        "chapter_count": len(chapters),
        "pub_date": album.pub_date,
        "update_date": album.update_date,
        "likes": album.likes,
        "views": album.views,
        "comment_count": album.comment_count,
        "chapters": chapters[:limit],
        "truncated": len(chapters) > limit,
    }


@mcp.tool(annotations=READ_ONLY)
def get_photo_detail(photo_id: str,
                     config_file: str = "",
                     ) -> Dict[str, Any]:
    """查单个章节详情：所属本子、章节序号、图片数。

    :param photo_id: 章节 id，如 '456' 或 'p456'
    :param config_file: 可选的 option.yml 绝对路径
    """
    try:
        photo = _client(config_file).get_photo_detail(photo_id)
    except JmcomicMcpError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.warning("get_photo_detail 失败: %s", e)
        return {"error": f"获取章节详情失败: {type(e).__name__}: {e}"}

    return {
        "photo_id": photo.photo_id,
        "album_id": photo.album_id,
        "title": photo.title,
        "index": photo.index,
        "image_count": len(photo),
    }


@mcp.tool(annotations=READ_ONLY)
def get_ranking(period: str = "day",
                category: str = "0",
                page: int = 1,
                limit: int = 20,
                config_file: str = "",
                ) -> Dict[str, Any]:
    """查排行榜（日榜 / 周榜 / 月榜，按观看数排）。

    :param period: day=日榜(默认) / week=周榜 / month=月榜
    :param category: 分类：0=全部(默认) / doujin / single / short / hanman / meiman / another
    :param page: 页码，从 1 开始
    :param limit: 最多返回多少条（1-50，默认 20）
    :param config_file: 可选的 option.yml 绝对路径
    """
    try:
        client = _client(config_file)
        category = category or "0"
        page = max(1, int(page))
        if period == "week":
            page_obj = client.week_ranking(page, category)
        elif period == "month":
            page_obj = client.month_ranking(page, category)
        else:
            page_obj = client.day_ranking(page, category)
    except JmcomicMcpError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.warning("get_ranking 失败: %s", e)
        return {"error": f"获取排行失败: {type(e).__name__}: {e}"}

    result = _page_items(page_obj, limit)
    result.update({"period": period, "category": category, "page": page})
    return result


@mcp.tool(annotations=READ_ONLY)
def get_favorites(folder_id: str = "0",
                  page: int = 1,
                  order_by: str = "mr",
                  limit: int = 20,
                  config_file: str = "",
                  ) -> Dict[str, Any]:
    """查自己的禁漫收藏夹（需要登录态）。

    登录态从 config_file 指向的 option.yml 来，在里面配 login 插件即可：

        plugins:
          after_init:
            - plugin: login
              kwargs:
                username: 你的用户名
                password: 你的密码

    option 构造时会自动登录并把 cookies 带上。没配登录态时这个工具会报错。

    :param folder_id: 收藏夹 id，'0' 表示全部
    :param page: 页码，从 1 开始
    :param order_by: mr=最新(默认) / mv=观看数 / mp=图片数 / tf=点赞数
    :param limit: 最多返回多少条（1-50，默认 20）
    :param config_file: option.yml 绝对路径（收藏夹必须提供）
    """
    try:
        page_obj = _client(config_file).favorite_folder(
            page=max(1, int(page)),
            order_by=order_by or "mr",
            folder_id=folder_id or "0",
        )
    except JmcomicMcpError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.warning("get_favorites 失败: %s", e)
        return {"error": f"获取收藏夹失败（通常是没有登录态）: {type(e).__name__}: {e}"}

    result = _page_items(page_obj, limit)
    result.update({
        "folder_id": folder_id,
        "page": page,
        "folders": [
            {"folder_id": fid, "name": fname}
            for fid, fname in page_obj.iter_folder_id_name()
        ],
    })
    return result


# ---------------------------------------------------------------------------
# 4. 下载任务工具
# ---------------------------------------------------------------------------

@mcp.tool(annotations=WRITE)
def start_download(album_ids: str = "",
                   photo_ids: str = "",
                   dest_dir: str = "",
                   image_suffix: str = "",
                   image_threads: int = 0,
                   config_file: str = "",
                   confirmed: bool = False,
                   ) -> Dict[str, Any]:
    """启动下载任务（会写磁盘 + 消耗对方服务器带宽，属于有副作用的操作）。

    **必须由用户明确同意后才能执行**：第一次调用请传 confirmed=False，
    本工具会返回 confirmation_required 和一份计划（下哪些号、存到哪、多少并发）；
    把计划念给用户、得到同意后，再用完全相同的参数 + confirmed=True 调用一次。

    本工具立即返回 task_id，不阻塞；进度用 get_task 查。

    :param album_ids: 本子 id，多个用空格/逗号分隔。如 '422866 430000'
    :param photo_ids: 章节 id，多个用空格/逗号分隔。如 '456' 或 'p456'
    :param dest_dir: 下载根目录，必须是绝对路径（支持 ~）。留空用 ~/Downloads/JMComic。
                     该值**始终覆盖**配置文件里的 dir_rule.base_dir。
    :param image_suffix: 图片格式，如 '.png' / '.jpg'。留空保持原格式。
    :param image_threads: 图片下载并发数（留空或 0 用 jmcomic 默认 30）
    :param config_file: 可选的 option.yml 绝对路径（域名/代理/cookies/登录插件）
    :param confirmed: 用户是否已明确同意本次下载
    """
    album_list, photo_list, unparsed = parse_targets(album_ids, photo_ids)

    if unparsed:
        return {
            "error": f"以下 id 无法解析: {unparsed}",
            "hint": "id 应形如 '422866'、'JM422866' 或章节号 'p456'",
        }

    if not album_list and not photo_list:
        return {"error": "没有可下载的目标：album_ids 与 photo_ids 都是空的"}

    try:
        resolved_dest = resolve_dest_dir(dest_dir)
    except JmcomicMcpError as e:
        return {"error": str(e)}

    threads = int(image_threads) if image_threads else None
    if threads is not None and threads < 1:
        return {"error": f"image_threads 必须 >= 1，收到 {image_threads}"}

    plan = {
        "albums": album_list,
        "photos": photo_list,
        "dest_dir": resolved_dest,
        "image_suffix": image_suffix or "保持原格式",
        "image_threads": threads or "jmcomic 默认(30)",
        "config_file": os.path.expanduser(config_file) if config_file else "默认配置",
        "total_targets": len(album_list) + len(photo_list),
    }

    if not confirmed:
        return {
            "status": "confirmation_required",
            "message": ("请先向用户确认这份下载计划（清单、目标目录、并发数），"
                        "得到明确同意后用相同参数加 confirmed=True 再调用一次。"),
            "plan": plan,
        }

    try:
        task = manager.submit(
            album_list,
            photo_list,
            dest_dir=resolved_dest,
            image_suffix=image_suffix or None,
            image_threads=threads,
            config_file=config_file or None,
        )
    except JmcomicMcpError as e:
        return {"error": str(e)}

    logger.info("任务已提交: %s -> %s", task.task_id, resolved_dest)
    return {
        "status": "started",
        "task_id": task.task_id,
        "plan": plan,
        "message": "任务已在后台启动，用 get_task 查询进度。",
        "task": task.snapshot(),
    }


@mcp.tool(annotations=READ_ONLY)
def get_task(task_id: str) -> Dict[str, Any]:
    """查单个下载任务的进度与结果。

    state 取值：pending 排队中 / running 下载中 / succeeded 全部成功 /
    partial 部分图片失败 / failed 失败 / cancelled 已取消。

    进度字段怎么读：

    - `progress_basis` 说明百分比是按什么算的：`photos`（章节）或 `images`（图片）。
      长篇多章本子会按章节算，因为图片总数要等每章开始才知道。
    - `total_known: false` 表示分母还没齐，`progress_percent` 只能当参考，
      以 `images` / `photos` 那两个字符串为准。

    结果字段怎么读：

    - `verified` 是**磁盘事实**（跑完之后对目录做 stat 得到的文件数/字节数/空文件数），
      `images` 那个 `219/219` 只是下载器的记账。两者不一致时以 `verified` 为准。
    - `created_dirs` 是本次任务实际写入过的目录清单。**要清理半成品就用它**，
      别靠"哪个目录的修改时间比较新"去猜——那种猜法会误伤之前下的东西。
    - 失败时看 `error_kind`（稳定枚举）和 `error_hint`（一句人话），
      `error` 里是原始堆栈、只在需要深挖时看。

    :param task_id: start_download 返回的任务 id
    """
    task = manager.get(task_id)
    if task is None:
        return {"error": f"任务不存在: {task_id}（服务重启后历史任务会清空）"}
    return task.snapshot()


@mcp.tool(annotations=READ_ONLY)
def list_tasks(limit: int = 20) -> Dict[str, Any]:
    """列出最近的下载任务（最新的在前）。

    返回的是精简快照：**不含 `created_dirs` 明细**（那可能几十条路径），
    需要目录清单时对具体任务调 get_task。

    :param limit: 最多返回多少条（默认 20）
    """
    limit = max(1, min(int(limit or 20), 100))
    tasks = manager.list(limit)
    return {
        "count": len(tasks),
        "tasks": [t.snapshot(include_created_dirs=False) for t in tasks],
    }


@mcp.tool(annotations=WRITE)
def cancel_task(task_id: str) -> Dict[str, Any]:
    """取消下载任务。

    语义是「不再开新的，收不回已经发出去的」：

    - 取消信号生效是**立即**的 —— 之后不再发起任何新的图片请求，剩余章节直接跳过；
    - 但当时已经在传输中的那批请求收不回来，它们会跑完，数量约等于当时的并发数；
    - 所以从"发出取消"到 `state` 真正变成 `cancelled` 有一段收尾时间，
      并发越高、单张越慢就越长（实测 8 线程下一次要几十秒）。

    期间用 get_task 看 `cancel_requested: true` 就知道信号已经吃到了。

    :param task_id: 要取消的任务 id
    """
    task = manager.cancel(task_id)
    if task is None:
        return {"error": f"任务不存在: {task_id}"}

    snapshot = task.snapshot()
    return {
        "status": "cancel_requested",
        "message": ("已发出取消信号：不会再发起新请求，但在途的那批会跑完，"
                    "之后 state 才变 cancelled。用 get_task 确认。"),
        "task": snapshot,
    }


def main() -> None:
    logger.info("JMComic MCP server 启动 (stdio)，默认下载目录: %s", DEFAULT_DEST_DIR)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

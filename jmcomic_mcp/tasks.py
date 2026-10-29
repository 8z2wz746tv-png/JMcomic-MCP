# -*- coding: utf-8 -*-
"""
JMComic 下载任务管理器。

设计要点
--------
1. 下载是长任务（一个本子可能几分钟），MCP 工具调用不能阻塞：
   提交后立刻返回 task_id，由查询工具轮询进度。
2. 取消要真的生效：CancellableDownloader 在章节/图片的调度点检查取消标志，
   取消后**不再发起任何新请求**；已经发出去的请求收不回来，会在途跑完
   （数量约等于当时的并发数），之后任务才进入终态。
3. 每个任务独立构造 JmOption，并把 dest_dir 显式写进 dir_rule.base_dir。
   漏掉这一步，jmcomic 会用 os.getcwd() 当下载根目录，文件落到 MCP server 进程的
   启动目录（宿主拉起的子进程 cwd 常常是 / 或宿主自己的目录），参数就等于在说谎。
"""
from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from jmcomic import create_option
from jmcomic.jm_downloader import JmDownloader
from jmcomic.jm_exception import (
    JmcomicException,
    MissingAlbumPhotoException,
    PartialDownloadFailedException,
    RequestRetryAllFailException,
)
from jmcomic.jm_option import JmOption
from jmcomic.jm_toolkit import JmcomicText

KIND_ALBUM = "album"
KIND_PHOTO = "photo"

STATE_PENDING = "pending"
STATE_RUNNING = "running"
STATE_SUCCEEDED = "succeeded"
STATE_PARTIAL = "partial"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

# 失败原因枚举：给调用方一个稳定的判断依据，不用去两百字堆栈里捞根因
ERROR_CANCELLED = "cancelled"
ERROR_NOT_FOUND = "not_found"
ERROR_CDN_DNS_BLACKHOLED = "image_cdn_dns_blackholed"
ERROR_CDN_DNS_FAILED = "image_cdn_dns_failed"
ERROR_CDN_UNREACHABLE = "image_cdn_unreachable"
ERROR_CDN_FAILED = "image_cdn_failed"
ERROR_NETWORK = "network_unreachable"
ERROR_CONFIG = "config_invalid"
ERROR_DEST_NOT_WRITABLE = "dest_dir_not_writable"
ERROR_UNKNOWN = "unknown"

TERMINAL_STATES = frozenset({STATE_SUCCEEDED, STATE_PARTIAL, STATE_FAILED, STATE_CANCELLED})

DEFAULT_DEST_DIR = os.path.join(os.path.expanduser("~"), "Downloads", "JMComic")
MAX_HISTORY = 50
ERROR_MAX_LEN = 600

# 并发闸门：单任务图片线程上限 + 同时运行的任务数上限。
# 不加这道闸，模型连着发三个 start_download 就是 3 × 30 = 90 个并发请求打同一个
# CDN，被站点限流后 classify_failure 只会报 image_cdn_unreachable（因为它查了
# DNS，域名是好的），把用户引去换 DNS 开代理 —— 那是误诊。
DEFAULT_IMAGE_THREADS = 8   # 不传 image_threads 时用它，替掉 jmcomic 默认的 30
MAX_IMAGE_THREADS = 16
DEFAULT_MAX_CONCURRENT_TASKS = 2


class JmcomicMcpError(Exception):
    """参数校验失败等可直接展示给调用方的错误。"""


# ---------------------------------------------------------------------------
# 失败归类：把 jmcomic 抛出来的那坨堆栈压成「一个枚举 + 一句人话」
# ---------------------------------------------------------------------------

DNS_RESOLVER = socket.getaddrinfo  # 测试里替换成假的，免得真去查 DNS


def _probe_host(host: str) -> str:
    """
    探测主机名的解析状态，返回 ok / blackholed / unresolvable。

    blackholed 指解析出来全是 0.0.0.0 / :: —— 域名还在但指向不可达地址，
    典型是被 DNS 层面做了过滤，连接根本建立不起来。
    """
    try:
        infos = DNS_RESOLVER(host, 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, OSError):
        return "unresolvable"

    ips = {info[4][0] for info in infos if info and info[4]}
    if not ips:
        return "unresolvable"
    if ips <= {"0.0.0.0", "::"}:
        return "blackholed"
    return "ok"


def _failed_image_hosts(downloader) -> List[str]:
    """从下载器记录的失败图片里提取去重后的域名。"""
    hosts: List[str] = []
    for item in getattr(downloader, "download_failed_image", None) or []:
        detail = item[0] if isinstance(item, tuple) else item
        url = getattr(detail, "download_url", None) or getattr(detail, "img_url", None)
        if not url:
            continue
        host = urlparse(str(url)).hostname
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def classify_failure(exc: BaseException, task: "DownloadTask") -> Tuple[str, str]:
    """
    把异常归类成 (kind, hint)。

    kind 是稳定枚举，调用方可以直接拿它做分支；
    hint 是给人看的一句话，说明「发生了什么 + 下一步做什么」。
    """
    if task.cancel_event.is_set():
        return ERROR_CANCELLED, "任务已被取消：不再发起新请求，已下载的文件保留在原地。"

    if isinstance(exc, PartialDownloadFailedException):
        try:
            downloader = exc.downloader
        except Exception:
            downloader = None

        failed_images = list(getattr(downloader, "download_failed_image", None) or [])
        failed_photos = list(getattr(downloader, "download_failed_photo", None) or [])

        if not failed_images and failed_photos:
            return (ERROR_NOT_FOUND,
                    f"{len(failed_photos)} 个章节获取失败，可能已下架或 id 有误。")

        hosts = _failed_image_hosts(downloader)
        if hosts:
            state = _probe_host(hosts[0])
            if state == "blackholed":
                return (ERROR_CDN_DNS_BLACKHOLED,
                        f"图片 CDN 域名 {hosts[0]} 被 DNS 解析到 0.0.0.0（DNS 黑洞），"
                        f"连接根本建立不起来 —— 换 DNS 或走代理后重试，不是号码的问题。")
            if state == "unresolvable":
                return (ERROR_CDN_DNS_FAILED,
                        f"图片 CDN 域名 {hosts[0]} 解析失败，检查本机 DNS / 代理设置。")
            return (ERROR_CDN_UNREACHABLE,
                    f"图片 CDN 域名 {hosts[0]} 能解析但连不上，可能是网络受限或站点故障。")

        return (ERROR_CDN_FAILED,
                f"{len(failed_images)} 张图片下载失败，但没有拿到可判断的域名信息，"
                f"看 error 字段的原始错误。")

    if isinstance(exc, MissingAlbumPhotoException):
        return (ERROR_NOT_FOUND,
                f"本子/章节不存在: {exc.error_jmid}，检查号码是否输错。")

    if isinstance(exc, RequestRetryAllFailException):
        return (ERROR_NETWORK, "所有候选域名都请求失败，检查网络或代理。")

    if isinstance(exc, PermissionError):
        return (ERROR_DEST_NOT_WRITABLE,
                f"下载目录不可写: {exc.filename or task.dest_dir}")

    if isinstance(exc, JmcomicMcpError):
        return (ERROR_CONFIG, str(exc))

    if isinstance(exc, JmcomicException):
        return (ERROR_UNKNOWN, f"jmcomic 报错: {exc}")

    return (ERROR_UNKNOWN, f"{type(exc).__name__}: {exc}")


def _iter_tokens(text: Optional[str]) -> List[str]:
    """按空格/逗号/分号/换行切分 id 文本。"""
    if not text:
        return []

    normalized = text
    for sep in (",", "，", "、", ";", "；", "\n", "\t", "\r"):
        normalized = normalized.replace(sep, " ")

    return normalized.split()


def looks_like_photo_id(token: str) -> bool:
    """'p456' / 'P456' 这种带 p 前缀的写法是章节 id。"""
    return len(token) > 1 and token[0] in ("p", "P") and token[1:].isdigit()


def parse_targets(album_text: Optional[str],
                  photo_text: Optional[str],
                  ) -> Tuple[List[str], List[str], List[str]]:
    """
    解析「本子ID」和「章节ID」两个输入，返回 (album_ids, photo_ids, 无法解析的片段)。

    输入框决定归属；唯一的例外是 album 框里写了带 p 前缀的号（'p456'），
    那会被改判为章节，免得把章节当本子去下。
    """
    album_ids: List[Tuple[str, str]] = []
    photo_ids: List[Tuple[str, str]] = []
    bad: List[str] = []
    seen: Dict[str, set] = {KIND_ALBUM: set(), KIND_PHOTO: set()}

    for want_photo, text in ((False, album_text), (True, photo_text)):
        for piece in _iter_tokens(text):
            is_photo = want_photo or looks_like_photo_id(piece)
            kind = KIND_PHOTO if is_photo else KIND_ALBUM
            # 'p456'：p 前缀要自己摘掉，parse_to_jm_id 只认数字/JM前缀/URL
            # （上游 cl.py 也是这么处理的）
            raw = piece[1:] if looks_like_photo_id(piece) else piece
            try:
                jmid = JmcomicText.parse_to_jm_id(raw)
            except Exception:
                bad.append(piece)
                continue
            # 同一个框里重复粘贴也要去重，否则同一章会排两次、
            # photos_total 虚高、还多跑一轮请求
            if jmid in seen[kind]:
                continue
            seen[kind].add(jmid)
            (photo_ids if is_photo else album_ids).append((kind, jmid))

    # 同一个号不要下两次
    album_set = {jmid for _, jmid in album_ids}
    return (
        [jmid for _, jmid in album_ids],
        [jmid for _, jmid in photo_ids if jmid not in album_set],
        bad,
    )


def _blocked_roots() -> List[str]:
    """不该往里拉本子的目录：盘符根、系统目录、家目录本身、AppData。"""
    home = os.path.expanduser("~")
    if os.name == "nt":
        drive = os.environ.get("SystemDrive") or "C:"
        return [
            os.path.join(drive + os.sep, "Windows"),
            os.path.join(drive + os.sep, "Program Files"),
            os.path.join(drive + os.sep, "Program Files (x86)"),
            os.path.join(drive + os.sep, "ProgramData"),
            os.path.join(home, "AppData"),
        ]
    return ["/etc", "/usr", "/bin", "/sbin", "/boot", "/dev", "/proc", "/sys",
            "/System", "/Library", "/Applications"]


def _under(path: str, parent: str) -> bool:
    # Windows 上路径大小写不敏感，commonpath 的结果却保留调用方的大小写，
    # 直接比对会让 'c:\windows\temp' 绕过 'C:\Windows' 的拦截，所以先 normcase。
    try:
        return (os.path.normcase(os.path.commonpath([path, parent]))
                == os.path.normcase(os.path.abspath(parent)))
    except (ValueError, OSError):
        return False


def reason_for_rejecting_dest(path: str) -> Optional[str]:
    """
    判断一个已规范化的绝对路径能不能当下载根目录。

    :return: 不能用的理由；能用则返回 None
    """
    if os.path.dirname(path) == path:
        return "指向磁盘根目录"
    if os.path.normcase(path) == os.path.normcase(os.path.expanduser("~")):
        return "指向用户主目录本身（会把文件散在家目录里），改用它的子目录"
    for root in _blocked_roots():
        if _under(path, root):
            return f"落在系统目录 {root} 内"
    return None


def resolve_dest_dir(dest_dir: Optional[str]) -> str:
    """
    把下载目录解析成绝对路径。

    刻意拒绝相对路径：MCP server 的 cwd 由宿主决定，相对路径会让文件落到
    不可预期的位置。支持 '~' 展开。

    也拒绝磁盘根、系统目录、家目录本身：dest_dir 是模型可控参数，
    没有白名单的话一次 typo 就能把一本 200 页的本子摊在 D:\\ 根目录。
    """
    if not dest_dir:
        return DEFAULT_DEST_DIR

    expanded = os.path.expanduser(dest_dir.strip())
    if not os.path.isabs(expanded):
        raise JmcomicMcpError(
            f"下载目录必须是绝对路径（支持 ~ 开头），收到: {dest_dir!r}。"
            f"建议使用 {DEFAULT_DEST_DIR}"
        )

    resolved = os.path.abspath(expanded)
    reason = reason_for_rejecting_dest(resolved)
    if reason:
        raise JmcomicMcpError(
            f"下载目录不能是 {resolved}：{reason}。"
            f"请给它一个专门的子目录，例如 {DEFAULT_DEST_DIR}"
        )

    return resolved


def build_option(dest_dir: str,
                 image_suffix: Optional[str] = None,
                 image_threads: Optional[int] = None,
                 config_file: Optional[str] = None,
                 ) -> JmOption:
    """
    构造本次下载使用的 JmOption。

    :param dest_dir: 下载根目录，**始终覆盖**配置里的 dir_rule.base_dir
    :param image_suffix: 图片后缀，如 '.png'；None 表示保持原格式
    :param image_threads: 图片下载并发数
    :param config_file: 可选的 option.yml 路径（域名/代理/cookies/登录插件等从这里来）
    """
    if config_file:
        path = os.path.expanduser(config_file.strip())
        if not os.path.isfile(path):
            raise JmcomicMcpError(f"配置文件不存在: {path}")
        option = create_option(path)
    else:
        option = JmOption.default()

    # 关键一行：下载目录必须落到 option 里才会生效
    option.dir_rule.base_dir = dest_dir

    if image_suffix:
        option.download.image.suffix = image_suffix

    if image_threads:
        option.download.threading.image = int(image_threads)

    return option


class _CancellableDownloader(JmDownloader):
    """
    在 jmcomic 的调度钩子上检查取消标志。

    - do_filter: 取消后让剩余章节/图片直接不进下载队列
    - download_by_image_detail: 取消后不再发起新的图片下载

    实测语义（不是"立刻断"）：标志一置位，新请求立刻停；当时已经派发出去的
    那批（约等于并发数）会跑完，任务随后进入终态。所以"生效"是立即的，
    "到终态"要等那批收尾。
    """

    def __init__(self, option: JmOption, task: "DownloadTask") -> None:
        super().__init__(option)
        self.task = task

    def do_filter(self, detail):
        if self.task.cancel_event.is_set():
            return []
        return detail

    def download_by_image_detail(self, image):
        if self.task.cancel_event.is_set():
            return
        return super().download_by_image_detail(image)

    # 下面是进度回调，全部跑在下载线程里

    def before_album(self, album):
        super().before_album(album)
        self.task.bind_album(album)

    def before_photo(self, photo):
        super().before_photo(photo)
        self.task.bind_photo(photo)

    def after_photo(self, photo):
        super().after_photo(photo)
        self.task.count_photo_done()

    def before_image(self, image, img_save_path):
        super().before_image(image, img_save_path)
        # 按实际保存路径的父目录记账：这个目录确实会被创建、确实会有文件落进去
        self.task.note_dir(os.path.dirname(img_save_path))
        # 已存在的图 after_image 不会被调用，这里先把"跳过"记下来，
        # 否则重下已下过的本子会出现「0 张也算成功」的困惑
        if getattr(image, "exists", False):
            self.task.count_skipped_existing()

    def after_image(self, image, img_save_path):
        super().after_image(image, img_save_path)
        self.task.count_image_done()


@dataclass
class DownloadTask:
    """一次下载任务的完整状态。字段读写都在锁内进行。"""

    task_id: str
    targets: List[Tuple[str, str]]  # [(kind, jmid)]
    dest_dir: str
    image_suffix: Optional[str] = None
    image_threads: Optional[int] = None
    config_file: Optional[str] = None

    state: str = STATE_PENDING
    current: str = ""
    error: Optional[str] = None
    error_kind: Optional[str] = None
    error_hint: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    albums_total: int = 0
    albums_done: int = 0
    photos_total: int = 0
    photos_done: int = 0
    images_total: int = 0
    images_done: int = 0
    # 本来就在磁盘上、被跳过的图片数。重下已下过的本子时，images 会显示 0/N，
    # 这个字段负责解释「为什么 0 张也算成功」
    skipped_existing: int = 0

    # 本次任务实际写入过的目录（去重）。取消/失败后要清理半成品时靠它，
    # 不用再去猜「哪个目录是新的」——那个猜法已经差点删错东西。
    created_dirs: List[str] = field(default_factory=list)
    # 结束后对 created_dirs 做一次真实 stat 的结果
    verified: Optional[Dict[str, Any]] = None

    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _thread: Optional[threading.Thread] = field(default=None, repr=False)
    _downloader: Optional[JmDownloader] = field(default=None, repr=False)
    _images_total_known: bool = field(default=False, repr=False)
    _dir_set: set = field(default_factory=set, repr=False)

    # ---------- 状态变更 ----------

    def mark_running(self) -> None:
        with self._lock:
            self.state = STATE_RUNNING
            self.started_at = time.time()

    def mark(self, state: str,
             error: Optional[str] = None,
             error_kind: Optional[str] = None,
             error_hint: Optional[str] = None) -> None:
        with self._lock:
            self.state = state
            if error:
                self.error = error[:ERROR_MAX_LEN]
            if error_kind:
                self.error_kind = error_kind
                self.error_hint = error_hint

    def set_current(self, text: str) -> None:
        with self._lock:
            self.current = text

    # ---------- 落地目录与核实 ----------

    def note_dir(self, path: str) -> None:
        """记录一个实际写入的目录（按图片保存路径的父目录来记）。"""
        if not path:
            return
        with self._lock:
            if path in self._dir_set:
                return
            self._dir_set.add(path)
            self.created_dirs.append(path)

    def verify_on_disk(self) -> Dict[str, Any]:
        """
        对 created_dirs 做一次真实 stat。

        工具自报的「219/219」只是下载器的记账，不是磁盘事实；这一步补上
        实际文件数、总字节和空文件数，调用方不用再自己去 find 一遍。
        """
        with self._lock:
            dirs = list(self.created_dirs)

        files = 0
        total_bytes = 0
        empty: List[str] = []

        for directory in dirs:
            for root, _subdirs, names in os.walk(directory):
                for name in names:
                    path = os.path.join(root, name)
                    try:
                        size = os.path.getsize(path)
                    except OSError:
                        continue
                    files += 1
                    total_bytes += size
                    if size == 0 and len(empty) < 50:
                        empty.append(path)

        result = {
            "dirs": len(dirs),
            "files": files,
            "bytes": total_bytes,
            "mb": round(total_bytes / 1024 / 1024, 1),
            "empty_files": len(empty),
            "empty_samples": empty,
        }
        with self._lock:
            self.verified = result
        return result

    # ---------- 进度 ----------

    def bind_album(self, album) -> None:
        with self._lock:
            self.albums_total += 1
            self.photos_total += len(album)
            # page_count 是整本的总页数，比逐章累加更早拿到，用它当总数进度更平滑。
            # 拿到之后就不能再逐章累加，否则会把同一批图片算两遍（进度封顶 50%）。
            page_count = int(getattr(album, "page_count", 0) or 0)
            if page_count > 0:
                self.images_total = page_count
                self._images_total_known = True

    def bind_photo(self, photo) -> None:
        with self._lock:
            if self.albums_total == 0:
                self.photos_total += 1
            if not self._images_total_known:
                self.images_total += len(photo)

    def count_photo_done(self) -> None:
        with self._lock:
            self.photos_done += 1

    def count_image_done(self) -> None:
        with self._lock:
            self.images_done += 1

    def count_skipped_existing(self) -> None:
        with self._lock:
            self.skipped_existing += 1

    def count_album_done(self) -> None:
        with self._lock:
            self.albums_done += 1

    # ---------- 快照 ----------

    def _decide_progress(self) -> Tuple[float, str, bool]:
        """
        算出 (百分比, 口径, 分母是否已完整)。

        分母完整与否决定该用什么口径：

        - 整本 page_count 拿到了 → 按图片算，分母一开始就是终值；
        - 拿不到但本子有多章 → **按章节算**。图片总分母是边下边累加的
          （每章开始时才知道这章多少页），用它算出来的百分比会虚高，
          而且分母一直涨、数字一直掉，长篇本子上会骗人；
        - 单章（或单独下一章）→ 回到按图片算。整章页数在章节绑定时一次性
          到手，分母立刻是终值，反而比"0/1 章"这种读法准。
        """
        if self.photos_total > 1 and not self._images_total_known:
            return round(self.photos_done * 100 / self.photos_total, 1), "photos", True

        if self.images_total:
            denominator_final = self._images_total_known or self.photos_total <= 1
            return round(self.images_done * 100 / self.images_total, 1), "images", denominator_final

        if self.state in (STATE_SUCCEEDED, STATE_PARTIAL):
            return 100.0, "none", True

        return 0.0, "none", False

    def snapshot(self, include_created_dirs: bool = True) -> Dict[str, Any]:
        with self._lock:
            percent, basis, total_known = self._decide_progress()

            end = self.finished_at or time.time()
            started = self.started_at or self.created_at

            data = {
                "task_id": self.task_id,
                "state": self.state,
                "dest_dir": self.dest_dir,
                "progress_percent": percent,
                # basis=photos 时百分比按章节算；total_known=False 表示分母还没齐，
                # 这个百分比只能当参考
                "progress_basis": basis,
                "total_known": total_known,
                "images": f"{self.images_done}/{self.images_total}" if self.images_total else None,
                "skipped_existing": self.skipped_existing,
                "photos": f"{self.photos_done}/{self.photos_total}" if self.photos_total else None,
                "albums": f"{self.albums_done}/{self.albums_total}" if self.albums_total else None,
                "current": self.current,
                "elapsed_sec": round(max(end - started, 0.0), 1),
                "error": self.error,
                "error_kind": self.error_kind,
                "error_hint": self.error_hint,
                "targets": [f"{kind}:{jmid}" for kind, jmid in self.targets],
                "cancel_requested": self.cancel_event.is_set(),
                "is_finished": self.state in TERMINAL_STATES,
                "created_dir_count": len(self.created_dirs),
                "verified": self.verified,
            }

            if include_created_dirs:
                data["created_dirs"] = list(self.created_dirs)

            return data

    def request_cancel(self) -> None:
        self.cancel_event.set()


class TaskManager:
    """进程内任务表。MCP server 是单进程长驻，所以内存态即可。"""

    def __init__(self,
                 max_history: int = MAX_HISTORY,
                 max_concurrent: int = DEFAULT_MAX_CONCURRENT_TASKS,
                 ) -> None:
        self._tasks: "OrderedDict[str, DownloadTask]" = OrderedDict()
        self._lock = threading.Lock()
        self._max_history = max_history
        # 同时在跑的任务数上限。0 或负数表示不限（测试/自建场景用）。
        self._max_concurrent = int(max_concurrent)
        self._slots = threading.BoundedSemaphore(max(1, self._max_concurrent))

    def _acquire_slot(self, task: DownloadTask) -> bool:
        """
        抢一个并发槽位。排队期间任务保持 pending 状态，取消能立刻生效。

        :return: True=拿到槽位；False=排队时被取消
        """
        if self._max_concurrent <= 0:
            return True
        task.set_current("排队中：等待并发下载槽位")
        while not task.cancel_event.is_set():
            if self._slots.acquire(timeout=0.2):
                return True
        return False

    def submit(self,
               album_ids: Sequence[str],
               photo_ids: Sequence[str],
               *,
               dest_dir: str,
               image_suffix: Optional[str] = None,
               image_threads: Optional[int] = None,
               config_file: Optional[str] = None,
               ) -> DownloadTask:
        targets: List[Tuple[str, str]] = [(KIND_ALBUM, i) for i in album_ids]
        targets += [(KIND_PHOTO, i) for i in photo_ids]
        if not targets:
            raise JmcomicMcpError("没有可下载的目标：album_ids 与 photo_ids 都是空的")

        task = DownloadTask(
            task_id=uuid.uuid4().hex[:12],
            targets=targets,
            dest_dir=dest_dir,
            image_suffix=image_suffix,
            image_threads=image_threads,
            config_file=config_file,
        )

        thread = threading.Thread(target=self._run, args=(task,),
                                  name=f"jmcomic-{task.task_id}", daemon=True)
        task._thread = thread

        with self._lock:
            self._tasks[task.task_id] = task
            self._evict_locked()

        thread.start()
        return task

    def get(self, task_id: str) -> Optional[DownloadTask]:
        with self._lock:
            return self._tasks.get(task_id)

    def list(self, limit: int = 20) -> List[DownloadTask]:
        with self._lock:
            tasks = list(self._tasks.values())
        tasks.reverse()  # 最新的在前
        return tasks[:max(1, limit)]

    def cancel(self, task_id: str) -> Optional[DownloadTask]:
        task = self.get(task_id)
        if task is None:
            return None
        task.request_cancel()
        return task

    def _evict_locked(self) -> None:
        """只淘汰已结束的旧任务，不碰正在跑的。"""
        if len(self._tasks) <= self._max_history:
            return
        for task_id in list(self._tasks.keys()):
            if len(self._tasks) <= self._max_history:
                break
            if self._tasks[task_id].state in TERMINAL_STATES:
                del self._tasks[task_id]

    # ---------- 工作线程 ----------

    def _run(self, task: DownloadTask) -> None:
        acquired = False
        try:
            # 先抢并发槽位，再进入 running —— 排队中的任务 state 保持 pending，
            # 这样 get_task 不会在还没开始下载时就谎报"下载中"。
            acquired = self._acquire_slot(task)
            if not acquired:
                task.mark(STATE_CANCELLED, error_kind=ERROR_CANCELLED,
                          error_hint="任务在排队等待并发槽位时被取消。")
                return

            task.mark_running()
            option = build_option(
                task.dest_dir,
                image_suffix=task.image_suffix,
                image_threads=task.image_threads,
                config_file=task.config_file,
            )
            os.makedirs(task.dest_dir, exist_ok=True)

            if task.cancel_event.is_set():
                task.mark(STATE_CANCELLED, error_kind=ERROR_CANCELLED,
                          error_hint="任务在开始前就被取消。")
                return

            with _CancellableDownloader(option, task) as dler:
                task._downloader = dler

                for kind, jmid in task.targets:
                    if task.cancel_event.is_set():
                        break

                    task.set_current(f"{kind} {jmid}")
                    if kind == KIND_ALBUM:
                        dler.download_album(jmid)
                        task.count_album_done()
                    else:
                        # 章节不计入 albums_done —— 曾经这里也调了 count_album_done，
                        # 导致 albums 字段出现 "3/1" 这种自相矛盾的进度
                        dler.download_photo(jmid)

                if task.cancel_event.is_set():
                    task.mark(STATE_CANCELLED, error_kind=ERROR_CANCELLED,
                              error_hint="任务已被取消：不再发起新请求，已下载的文件保留在原地。")
                    return

                dler.raise_if_has_exception()

            task.mark(STATE_SUCCEEDED)
            task.set_current("")

        except BaseException as e:  # noqa: BLE001 - 要把一切失败都归类，不能漏
            kind, hint = classify_failure(e, task)
            state = STATE_CANCELLED if kind == ERROR_CANCELLED else (
                STATE_PARTIAL if isinstance(e, PartialDownloadFailedException) else STATE_FAILED
            )
            task.mark(state, error=f"{type(e).__name__}: {e}", error_kind=kind, error_hint=hint)
        finally:
            if acquired:
                self._slots.release()
            task.finished_at = time.time()
            task._downloader = None
            try:
                task.verify_on_disk()
            except Exception:
                pass

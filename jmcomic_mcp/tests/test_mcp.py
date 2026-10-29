# -*- coding: utf-8 -*-
"""
jmcomic_mcp 的离线自测：不联网，全部用替身。

跑法： ./venv/bin/python -m unittest discover -s jmcomic_mcp/tests -v
"""
import os
import sys
import threading
import time
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_ROOT, os.path.join(_ROOT, "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from jmcomic_mcp import tasks as T  # noqa: E402


class TestParseTargets(unittest.TestCase):

    def test_mixed_separators(self):
        albums, photos, bad = T.parse_targets("422866, 430000\n431111", "")
        self.assertEqual(albums, ["422866", "430000", "431111"])
        self.assertEqual(photos, [])
        self.assertEqual(bad, [])

    def test_photo_box_wins(self):
        albums, photos, _ = T.parse_targets("", "456 789")
        self.assertEqual(albums, [])
        self.assertEqual(photos, ["456", "789"])

    def test_p_prefix_in_album_box_is_rerouted(self):
        albums, photos, _ = T.parse_targets("422866 p456", "")
        self.assertEqual(albums, ["422866"])
        self.assertEqual(photos, ["456"])

    def test_jm_prefix_and_url(self):
        albums, _, bad = T.parse_targets("JM422866 https://18comic.vip/album/430000/", "")
        self.assertIn("422866", albums)
        self.assertIn("430000", albums)
        self.assertEqual(bad, [])

    def test_unparsable_collected(self):
        albums, _, bad = T.parse_targets("422866 abc", "")
        self.assertEqual(albums, ["422866"])
        self.assertEqual(bad, ["abc"])

    def test_duplicate_across_boxes_deduped(self):
        albums, photos, _ = T.parse_targets("422866", "422866")
        self.assertEqual(albums, ["422866"])
        self.assertEqual(photos, [])

    def test_duplicate_inside_same_box_deduped(self):
        # 重复粘贴同一批号：不去重就会排两次、photos_total 虚高、多跑一轮请求
        albums, _, _ = T.parse_targets("422866 422866 JM422866", "")
        self.assertEqual(albums, ["422866"])
        _, photos, _ = T.parse_targets("", "456 456 p456 789")
        self.assertEqual(photos, ["456", "789"])


class TestDestDir(unittest.TestCase):

    def test_default(self):
        self.assertEqual(T.resolve_dest_dir(""), T.DEFAULT_DEST_DIR)
        self.assertEqual(T.resolve_dest_dir(None), T.DEFAULT_DEST_DIR)

    def test_tilde_expanded(self):
        got = T.resolve_dest_dir("~/JMComicTest")
        self.assertTrue(os.path.isabs(got))
        self.assertNotIn("~", got)
        self.assertTrue(got.endswith("JMComicTest"))

    def test_relative_rejected(self):
        with self.assertRaises(T.JmcomicMcpError):
            T.resolve_dest_dir("downloads/jm")

    def test_filesystem_root_rejected(self):
        with self.assertRaises(T.JmcomicMcpError):
            T.resolve_dest_dir(os.path.abspath(os.sep))

    def test_home_itself_rejected(self):
        with self.assertRaises(T.JmcomicMcpError):
            T.resolve_dest_dir("~")

    def test_nested_dest_under_home_allowed(self):
        got = T.resolve_dest_dir("~/Downloads/JMComic")
        self.assertEqual(got, os.path.join(os.path.expanduser("~"), "Downloads", "JMComic"))

    @unittest.skipUnless(os.name == "nt", "Windows 路径大小写不敏感")
    def test_system_dir_rejected_case_insensitively(self):
        lowered = os.path.join(os.environ.get("SystemDrive", "C:") + os.sep,
                               "windows", "temp", "jm")
        with self.assertRaises(T.JmcomicMcpError):
            T.resolve_dest_dir(lowered)


class TestBuildOption(unittest.TestCase):
    """核心回归：dest_dir 必须真的写进 option，否则文件会落到进程 cwd。"""

    def test_base_dir_applied(self):
        dest = os.path.join(os.path.expanduser("~"), "JMComicTestDir")
        option = T.build_option(dest)
        self.assertEqual(option.dir_rule.base_dir, os.path.abspath(dest))
        # 不是 cwd 才算真的生效
        self.assertNotEqual(option.dir_rule.base_dir, os.getcwd())

    def test_suffix_and_threads_applied(self):
        option = T.build_option(T.DEFAULT_DEST_DIR, image_suffix=".png", image_threads=7)
        self.assertEqual(option.download.image.suffix, ".png")
        self.assertEqual(option.decide_image_batch_count(None), 7)

    def test_missing_config_file_rejected(self):
        with self.assertRaises(T.JmcomicMcpError):
            T.build_option(T.DEFAULT_DEST_DIR, config_file="/no/such/option.yml")


class _FakeDownloader:
    """替身下载器：模拟真实下载器的接口与耗时，不联网、不写图。"""

    def __init__(self, option, task, fail=False):
        self.option = option
        self.task = task
        self.fail = fail
        self.started = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _work(self):
        for _ in range(20):
            if self.task.cancel_event.is_set():
                return
            self.started += 1
            self.task.count_image_done()
            time.sleep(0.02)

    def download_album(self, album_id):
        self.task.albums_total += 1
        self.task.images_total += 20
        self._work()

    def download_photo(self, photo_id):
        self.task.photos_total += 1
        self.task.images_total += 20
        self._work()

    def raise_if_has_exception(self):
        if self.fail:
            raise T.PartialDownloadFailedException("替身失败", {"downloader": self})


class _FakeFactory:
    def __init__(self, fail=False):
        self.fail = fail

    def __call__(self, option, task):
        return _FakeDownloader(option, task, fail=self.fail)


class TestTaskManager(unittest.TestCase):

    def setUp(self):
        self._real_downloader = T._CancellableDownloader
        # 用临时目录：测试环境（沙箱）通常不允许写用户主目录
        self._tmp_dirs = []

    def tearDown(self):
        T._CancellableDownloader = self._real_downloader
        import shutil
        for d in self._tmp_dirs:
            shutil.rmtree(d, ignore_errors=True)

    def _tmp_dir(self):
        import tempfile
        d = tempfile.mkdtemp(prefix="jmcomic-mcp-test-")
        self._tmp_dirs.append(d)
        return d

    def _submit(self, **kwargs):
        manager = T.TaskManager()
        task = manager.submit(["422866"], [], dest_dir=self._tmp_dir(), **kwargs)
        return manager, task

    def _wait(self, task):
        for _ in range(300):
            if task.snapshot()["is_finished"]:
                break
            time.sleep(0.02)
        return task.snapshot()

    def test_success_path(self):
        T._CancellableDownloader = _FakeFactory()
        manager, task = self._submit()
        snap = self._wait(task)
        self.assertEqual(snap["state"], T.STATE_SUCCEEDED, snap)
        self.assertEqual(snap["progress_percent"], 100.0)
        self.assertEqual(snap["albums"], "1/1")
        self.assertEqual(len(manager.list(10)), 1)

    def test_cancel_stops_early(self):
        T._CancellableDownloader = _FakeFactory()
        manager, task = self._submit()
        time.sleep(0.05)
        manager.cancel(task.task_id)
        snap = self._wait(task)
        self.assertEqual(snap["state"], T.STATE_CANCELLED, snap)
        self.assertLess(snap["progress_percent"], 100.0)
        self.assertTrue(snap["cancel_requested"])

    def test_partial_reported_as_partial(self):
        T._CancellableDownloader = _FakeFactory(fail=True)
        manager, task = self._submit()
        snap = self._wait(task)
        self.assertEqual(snap["state"], T.STATE_PARTIAL, snap)
        self.assertIn("替身失败", snap["error"])

    def test_dest_dir_flows_into_snapshot_and_disk(self):
        T._CancellableDownloader = _FakeFactory()
        dest = self._tmp_dir()
        manager = T.TaskManager()
        task = manager.submit(["422866"], [], dest_dir=dest)
        snap = self._wait(task)
        self.assertEqual(snap["state"], T.STATE_SUCCEEDED, snap)
        self.assertEqual(snap["dest_dir"], dest)
        self.assertTrue(os.path.isdir(dest), "下载目录应该被创建出来")

    def test_empty_targets_rejected(self):
        manager = T.TaskManager()
        with self.assertRaises(T.JmcomicMcpError):
            manager.submit([], [], dest_dir=T.DEFAULT_DEST_DIR)

    def test_unknown_task(self):
        self.assertIsNone(T.TaskManager().get("nope"))
        self.assertIsNone(T.TaskManager().cancel("nope"))

    def test_photo_target_does_not_count_album(self):
        # 回归：章节分支曾经误调 count_album_done()，进度会冒出 "3/1"
        T._CancellableDownloader = _FakeFactory()
        manager = T.TaskManager()
        task = manager.submit([], ["456", "789"], dest_dir=self._tmp_dir())
        snap = self._wait(task)
        self.assertEqual(snap["state"], T.STATE_SUCCEEDED, snap)
        self.assertIsNone(snap["albums"], snap["albums"])
        self.assertEqual(task.albums_done, 0)

    def test_mixed_targets_album_count_not_inflated(self):
        T._CancellableDownloader = _FakeFactory()
        manager = T.TaskManager()
        task = manager.submit(["422866"], ["456", "789"], dest_dir=self._tmp_dir())
        snap = self._wait(task)
        self.assertEqual(snap["albums"], "1/1", snap["albums"])

    def test_concurrent_slots_queue_second_task(self):
        T._CancellableDownloader = _FakeFactory()
        manager = T.TaskManager(max_concurrent=1)
        dest = self._tmp_dir()
        first = manager.submit(["111"], [], dest_dir=dest)
        second = manager.submit(["222"], [], dest_dir=dest)
        time.sleep(0.1)
        queued = second.snapshot()
        self.assertEqual(queued["state"], T.STATE_PENDING, queued["state"])
        self.assertIn("排队", queued["current"] or "")
        self.assertEqual(self._wait(first)["state"], T.STATE_SUCCEEDED)
        self.assertEqual(self._wait(second)["state"], T.STATE_SUCCEEDED)

    def test_cancel_while_queued(self):
        T._CancellableDownloader = _FakeFactory()
        manager = T.TaskManager(max_concurrent=1)
        dest = self._tmp_dir()
        first = manager.submit(["111"], [], dest_dir=dest)
        second = manager.submit(["222"], [], dest_dir=dest)
        time.sleep(0.05)
        manager.cancel(second.task_id)
        self._wait(first)
        self.assertEqual(self._wait(second)["state"], T.STATE_CANCELLED)
        self.assertEqual(second.albums_total, 0, "排队中被取消的任务不该真的开始下载")

    def test_slot_released_after_failure(self):
        T._CancellableDownloader = _FakeFactory(fail=True)
        manager = T.TaskManager(max_concurrent=1)
        dest = self._tmp_dir()
        first = manager.submit(["111"], [], dest_dir=dest)
        self.assertEqual(self._wait(first)["state"], T.STATE_PARTIAL)
        T._CancellableDownloader = _FakeFactory()
        second = manager.submit(["222"], [], dest_dir=dest)
        self.assertEqual(self._wait(second)["state"], T.STATE_SUCCEEDED,
                         "失败任务必须释放槽位，否则后面的任务永远排队")


class TestProgressAccounting(unittest.TestCase):
    """回归：图片总数不能把整本 page_count 和逐章 len(photo) 算两遍。"""

    class _Album:
        def __init__(self, page_count, chapters):
            self.page_count = page_count
            self._chapters = chapters

        def __len__(self):
            return self._chapters

    class _Photo:
        def __init__(self, size):
            self._size = size

        def __len__(self):
            return self._size

    def _task(self):
        return T.DownloadTask(task_id="t", targets=[("album", "1")], dest_dir="/tmp/x")

    def test_album_page_count_is_authoritative(self):
        task = self._task()
        task.bind_album(self._Album(page_count=40, chapters=2))
        task.bind_photo(self._Photo(20))
        task.bind_photo(self._Photo(20))
        self.assertEqual(task.snapshot()["images"], "0/40")

    def test_falls_back_to_per_photo_when_page_count_missing(self):
        task = self._task()
        task.bind_album(self._Album(page_count=0, chapters=2))
        task.bind_photo(self._Photo(3))
        task.bind_photo(self._Photo(4))
        self.assertEqual(task.snapshot()["images"], "0/7")

    def test_standalone_photo_task(self):
        task = T.DownloadTask(task_id="t", targets=[("photo", "9")], dest_dir="/tmp/x")
        task.bind_photo(self._Photo(5))
        self.assertEqual(task.snapshot()["images"], "0/5")
        self.assertEqual(task.snapshot()["photos"], "0/1")

    # ---- 百分比口径：分母不完整时不能按图片算，否则长篇本子进度虚高 ----

    def test_multi_chapter_uses_photo_basis(self):
        """63 章那种：图片分母边下边长，必须按章节算，否则头一分钟就显示 87%。"""
        task = self._task()
        task.bind_album(self._Album(page_count=0, chapters=63))
        task.bind_photo(self._Photo(200))
        task.bind_photo(self._Photo(200))
        task.count_photo_done()

        snap = task.snapshot()
        self.assertEqual(snap["progress_basis"], "photos")
        self.assertTrue(snap["total_known"])
        # 1/63 章 = 1.6%；旧口径会算成 200/400 = 50%
        self.assertEqual(snap["progress_percent"], round(100 / 63, 1))

    def test_page_count_known_uses_image_basis(self):
        """整本 page_count 拿到了就用图片口径：分母一开始就是终值。"""
        task = self._task()
        task.bind_album(self._Album(page_count=40, chapters=2))
        task.bind_photo(self._Photo(20))

        snap = task.snapshot()
        self.assertEqual(snap["progress_basis"], "images")
        self.assertTrue(snap["total_known"])

    def test_single_chapter_uses_image_basis(self):
        """单章多图：整章页数一次性到手，按图片算比"0/1 章"准。"""
        task = self._task()
        task.bind_album(self._Album(page_count=0, chapters=1))
        task.bind_photo(self._Photo(241))

        snap = task.snapshot()
        self.assertEqual(snap["progress_basis"], "images")
        self.assertTrue(snap["total_known"])
        self.assertEqual(snap["progress_percent"], 0.0)

    def test_before_anything_bound_denominator_unknown(self):
        task = self._task()
        snap = task.snapshot()
        self.assertEqual(snap["progress_basis"], "none")
        self.assertFalse(snap["total_known"])
        self.assertEqual(snap["progress_percent"], 0.0)


class TestCreatedDirs(unittest.TestCase):
    """清理半成品靠 created_dirs，不靠猜「哪个目录新」。"""

    def test_note_dir_dedup(self):
        task = T.DownloadTask(task_id="t", targets=[("album", "1")], dest_dir="/tmp/x")
        task.note_dir("/tmp/x/a")
        task.note_dir("/tmp/x/a")
        task.note_dir("/tmp/x/b")
        task.note_dir("")
        snap = task.snapshot()
        self.assertEqual(snap["created_dirs"], ["/tmp/x/a", "/tmp/x/b"])
        self.assertEqual(snap["created_dir_count"], 2)

    def test_snapshot_can_omit_dirs(self):
        task = T.DownloadTask(task_id="t", targets=[("album", "1")], dest_dir="/tmp/x")
        task.note_dir("/tmp/x/a")
        snap = task.snapshot(include_created_dirs=False)
        self.assertNotIn("created_dirs", snap)
        self.assertEqual(snap["created_dir_count"], 1)


class TestVerifyOnDisk(unittest.TestCase):
    """工具自报的 N/N 是记账，verified 才是磁盘事实。"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="jmcomic-verify-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _task(self):
        return T.DownloadTask(task_id="t", targets=[("album", "1")], dest_dir=self.tmp)

    def test_counts_real_files(self):
        sub = os.path.join(self.tmp, "album-a")
        os.makedirs(sub)
        for i in range(3):
            with open(os.path.join(sub, f"{i}.webp"), "wb") as f:
                f.write(b"x" * 10)

        task = self._task()
        task.note_dir(sub)
        task.images_done = 3
        result = task.verify_on_disk()

        self.assertEqual(result["dirs"], 1)
        self.assertEqual(result["files"], 3)
        self.assertEqual(result["bytes"], 30)
        self.assertEqual(result["empty_files"], 0)
        self.assertEqual(task.snapshot()["verified"]["files"], 3)

    def test_detects_empty_files_and_mismatch(self):
        sub = os.path.join(self.tmp, "album-b")
        os.makedirs(sub)
        open(os.path.join(sub, "00001.webp"), "wb").close()      # 0 字节
        with open(os.path.join(sub, "00002.webp"), "wb") as f:
            f.write(b"ok")

        task = self._task()
        task.note_dir(sub)
        task.images_done = 5  # 记账说 5 张，磁盘只有 2 个
        result = task.verify_on_disk()

        self.assertEqual(result["files"], 2)
        self.assertEqual(result["empty_files"], 1)

    def test_missing_dir_is_not_an_error(self):
        task = self._task()
        task.note_dir(os.path.join(self.tmp, "never-created"))
        result = task.verify_on_disk()
        self.assertEqual(result["files"], 0)
        self.assertEqual(result["dirs"], 1)


class _FakeImage:
    """只提供 classify_failure 需要的 download_url。"""

    def __init__(self, url):
        self.download_url = url


class _FailingDownloader:
    def __init__(self, urls=(), photos=0):
        self.download_failed_image = [(_FakeImage(u), RuntimeError("x")) for u in urls]
        self.download_failed_photo = [(None, RuntimeError("y"))] * photos


class TestErrorClassification(unittest.TestCase):
    """把堆栈压成「枚举 + 一句人话」；DNS 探测被替换成假的，不真去查。"""

    def setUp(self):
        self._real_resolver = T.DNS_RESOLVER
        self.task = T.DownloadTask(task_id="t", targets=[("album", "1")], dest_dir="/tmp/x")

    def tearDown(self):
        T.DNS_RESOLVER = self._real_resolver

    def _partial(self, urls=(), photos=0):
        dler = _FailingDownloader(urls, photos)
        return T.PartialDownloadFailedException("dler 失败", {"downloader": dler})

    def _fake_dns(self, ips=None, error=None):
        def resolver(host, *a, **kw):
            if error is not None:
                raise error
            return [(None, None, None, "", (ip, 0)) for ip in ips]
        T.DNS_RESOLVER = resolver

    def test_dns_blackholed(self):
        self._fake_dns(ips=["0.0.0.0"])
        kind, hint = T.classify_failure(
            self._partial(["https://cdn-msp.example.net/a.webp"]), self.task)
        self.assertEqual(kind, T.ERROR_CDN_DNS_BLACKHOLED)
        self.assertIn("0.0.0.0", hint)
        self.assertIn("cdn-msp.example.net", hint)

    def test_dns_unresolvable(self):
        import socket
        self._fake_dns(error=socket.gaierror("nope"))
        kind, hint = T.classify_failure(
            self._partial(["https://cdn.example.net/a.webp"]), self.task)
        self.assertEqual(kind, T.ERROR_CDN_DNS_FAILED)
        self.assertIn("解析失败", hint)

    def test_dns_ok_but_unreachable(self):
        self._fake_dns(ips=["1.2.3.4"])
        kind, _hint = T.classify_failure(
            self._partial(["https://cdn.example.net/a.webp"]), self.task)
        self.assertEqual(kind, T.ERROR_CDN_UNREACHABLE)

    def test_photo_only_failure_is_not_found(self):
        kind, hint = T.classify_failure(self._partial(photos=2), self.task)
        self.assertEqual(kind, T.ERROR_NOT_FOUND)
        self.assertIn("2 个章节", hint)

    def test_missing_album(self):
        exc = T.MissingAlbumPhotoException("没有", {"missing_jm_id": "999"})
        kind, hint = T.classify_failure(exc, self.task)
        self.assertEqual(kind, T.ERROR_NOT_FOUND)
        self.assertIn("999", hint)

    def test_permission_error(self):
        exc = PermissionError(13, "denied", "/nope")
        kind, hint = T.classify_failure(exc, self.task)
        self.assertEqual(kind, T.ERROR_DEST_NOT_WRITABLE)
        self.assertIn("/nope", hint)

    def test_cancelled_wins_over_other_errors(self):
        self.task.cancel_event.set()
        kind, _hint = T.classify_failure(RuntimeError("随便什么错"), self.task)
        self.assertEqual(kind, T.ERROR_CANCELLED)

    def test_unknown_falls_through(self):
        kind, hint = T.classify_failure(ValueError("怪事"), self.task)
        self.assertEqual(kind, T.ERROR_UNKNOWN)
        self.assertIn("ValueError", hint)


class TestServerTools(unittest.TestCase):
    """只测不需要联网就能确定的行为（确认门、参数校验）。"""

    def setUp(self):
        import jmcomic_mcp.server as S
        self.S = S

    def test_start_download_requires_confirmation(self):
        got = self.S.start_download(album_ids="422866")
        self.assertEqual(got["status"], "confirmation_required")
        self.assertEqual(got["plan"]["albums"], ["422866"])
        self.assertEqual(got["plan"]["dest_dir"], self.S.DEFAULT_DEST_DIR)
        self.assertNotIn("task_id", got)

    def test_start_download_empty_targets(self):
        got = self.S.start_download()
        self.assertIn("error", got)

    def test_start_download_bad_id(self):
        got = self.S.start_download(album_ids="not-an-id!!")
        self.assertIn("error", got)

    def test_start_download_relative_dir_rejected(self):
        got = self.S.start_download(album_ids="422866", dest_dir="relative/path", confirmed=True)
        self.assertIn("error", got)

    def test_start_download_bad_threads(self):
        got = self.S.start_download(album_ids="422866", image_threads=-1, confirmed=True)
        self.assertIn("error", got)

    def test_start_download_threads_capped(self):
        got = self.S.start_download(album_ids="422866", image_threads=99)
        self.assertIn("error", got)
        self.assertIn(str(self.S.MAX_IMAGE_THREADS), got["error"])

    def test_start_download_default_threads(self):
        # 不传 image_threads 时落到 8，不再让 jmcomic 用它的默认 30
        got = self.S.start_download(album_ids="422866")
        self.assertEqual(got["plan"]["image_threads"], self.S.DEFAULT_IMAGE_THREADS)

    def test_start_download_root_dir_rejected(self):
        got = self.S.start_download(album_ids="422866", dest_dir=os.path.abspath(os.sep))
        self.assertIn("error", got)

    def test_get_task_unknown(self):
        self.assertIn("error", self.S.get_task("nope"))

    def test_list_tasks_empty(self):
        self.assertEqual(self.S.list_tasks()["count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

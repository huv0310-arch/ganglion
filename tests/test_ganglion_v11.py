"""神经节 v1.1 测试 —— 四条改骨各至少 1 测 + v0 的 19 条语义兼容。

跑法:  python -m pytest tests/ -q
原件 tests/test_ganglion.py 一条不改，继续对 ganglion.py 跑。
"""

import json
import os
import re
import sys
import tempfile
import shutil
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ganglion_v11
from ganglion_v11 import Ganglion, _make_key, _DEFAULT_TTL

_TMP_LOG_DIR = None
_OLD_LOG = None


def setUpModule():
    """把留痕文件挪进临时目录，跑完不污染仓根。"""
    global _TMP_LOG_DIR, _OLD_LOG
    _TMP_LOG_DIR = tempfile.mkdtemp(prefix="gv11_log_")
    _OLD_LOG = ganglion_v11._ERROR_LOG
    ganglion_v11._ERROR_LOG = os.path.join(_TMP_LOG_DIR, "ganglion_v11.error.log")


def tearDownModule():
    ganglion_v11._ERROR_LOG = _OLD_LOG
    if _TMP_LOG_DIR:
        shutil.rmtree(_TMP_LOG_DIR, ignore_errors=True)


def _read_log():
    try:
        with open(ganglion_v11._ERROR_LOG, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return ""


# ══════════ ① 并发写保护 ══════════

def _writer(args):
    """子进程干活：独立实例、同一文件缓存，连写 n 条。留痕写进临时目录，不落仓根。"""
    path, wid, n = args
    import ganglion_v11
    ganglion_v11._ERROR_LOG = os.path.join(os.path.dirname(path), "ganglion_v11.error.log")
    ok = 0
    for i in range(n):
        g = Ganglion(path=path)
        g.check("k-%d-%d" % (wid, i), processor=lambda c: (1, "pid%s" % wid))
        ok += 1
    return ok


class TestConcurrentWrite(unittest.TestCase):
    """多进程 load→改→save 窗口不互覆丢写。"""

    def test_two_instances_share_file(self):
        """最小复现：A 写完 B 能复用到，锁内重读不吞掉别人的结果。"""
        path = os.path.join(_TMP_LOG_DIR, "share.json")
        g1 = Ganglion(path=path)
        g1.check("同一件事", processor=lambda c: (1, "判过"))
        g2 = Ganglion(path=path)
        verdict, reason = g2.check("同一件事", processor=lambda c: (0, "不该重判"))
        self.assertEqual(verdict, 1)
        self.assertIn("[记忆:", reason)

    def test_multi_process_no_lost_write(self):
        """4 进程 × 15 条 = 60 条，一条都不许丢。"""
        from concurrent.futures import ProcessPoolExecutor
        path = os.path.join(_TMP_LOG_DIR, "race.json")
        jobs = [(path, w, 15) for w in range(4)]
        with ProcessPoolExecutor(max_workers=4) as pool:
            done = list(pool.map(_writer, jobs))
        self.assertEqual(sum(done), 60)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(len(data), 60, "并发写丢数据了！")
        # 落盘是原子替换，不该留下半截临时文件
        leftovers = [f for f in os.listdir(_TMP_LOG_DIR) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        # 读写同开撞 os.replace（WinError 5）会丢写：一个 store 异常都不许有
        self.assertNotIn("store:race.json", _read_log())

    def test_corrupt_file_fails_open(self):
        """缓存文件被写坏 → 当空重来，留痕，不抛。"""
        path = os.path.join(_TMP_LOG_DIR, "broken.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ 这不是 json")
        g = Ganglion(path=path)
        verdict, reason = g.check("恢复后第一条", processor=lambda c: (1, "照常"))
        self.assertEqual(verdict, 1)
        self.assertEqual(reason, "照常")
        self.assertIn("load:broken.json", _read_log())


# ══════════ ② 吞错留痕 ══════════

class TestErrorTrail(unittest.TestCase):
    """fail-open 语义不变（绝不抛），但异常必须落 ganglion_v11.error.log。"""

    def test_fail_open_writes_log(self):
        """processor 炸 → 返回 (None, 理由) 不抛，日志里有时间戳+动作+异常类型。"""
        g = Ganglion(mem={})
        verdict, reason = g.check("会炸的", processor=lambda c: 1 / 0)
        self.assertIsNone(verdict)
        self.assertIn("检测异常", reason)
        line = _read_log().strip().splitlines()[-1]
        self.assertRegex(line, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \| ")
        self.assertIn("check:", line)
        self.assertIn("ZeroDivisionError", line)

    def test_log_failure_never_raises(self):
        """留痕路径本身不可写 → 也绝不把异常抛回调用方。"""
        g = Ganglion(mem={})
        old = ganglion_v11._ERROR_LOG
        ganglion_v11._ERROR_LOG = os.path.join(_TMP_LOG_DIR, "no_such_dir", "x.log")
        try:
            verdict, reason = g.check("炸且记不上", processor=lambda c: 1 / 0)
        finally:
            ganglion_v11._ERROR_LOG = old
        self.assertIsNone(verdict)
        self.assertIn("检测异常", reason)

    def test_exception_not_written_to_mem(self):
        """留痕不等于记录：异常判决仍然不写缓存（降级不冻结）。"""
        mem = {}
        Ganglion(mem=mem).check("会炸的", processor=lambda c: 1 / 0)
        self.assertEqual(mem, {})


# ══════════ ③ 默认 ttl 3600 → 1800 ══════════

class TestDefaultTTL(unittest.TestCase):
    """默认保鲜期 1800：过时结果更快让位（回归钉住默认值）。"""

    def test_default_is_1800(self):
        self.assertEqual(_DEFAULT_TTL, 1800)
        self.assertEqual(Ganglion(mem={}).ttl, 1800)

    def test_1799s_still_hits(self):
        """窗内（1800s 以内）照常复用。"""
        mem = {}
        g = Ganglion(mem=mem)
        g.check("半小时内的", processor=lambda c: (1, "第一版"))
        key = _make_key("半小时内的")
        v, r, t = mem[key]
        mem[key] = (v, r, time.time() - 1799)
        verdict, reason = g.check("半小时内的", processor=lambda c: (0, "不该重判"))
        self.assertEqual(verdict, 1)
        self.assertIn("[记忆:", reason)

    def test_1800s_expired_reruns(self):
        """过窗就重判。"""
        mem = {}
        g = Ganglion(mem=mem)
        g.check("过期的", processor=lambda c: (1, "第一版"))
        key = _make_key("过期的")
        v, r, t = mem[key]
        mem[key] = (v, r, time.time() - 1801)
        verdict, reason = g.check("过期的", processor=lambda c: (0, "第二版"))
        self.assertEqual(verdict, 0)
        self.assertEqual(reason, "第二版")


# ══════════ ④ 复用带年龄 ══════════

class TestBorrowedAge(unittest.TestCase):
    """复用旧判决时给出它的年龄，调用方自判信不信。"""

    def test_miss_has_no_age(self):
        g = Ganglion(mem={})
        g.check("新题", processor=lambda c: (1, "刚判"))
        self.assertIsNone(g.last_age)

    def test_hit_carries_age(self):
        mem = {}
        g = Ganglion(mem=mem)
        g.check("旧条目", processor=lambda c: (1, "旧判"))
        key = _make_key("旧条目")
        v, r, t = mem[key]
        mem[key] = (v, r, time.time() - 500)
        verdict, reason = g.check("旧条目", processor=lambda c: (0, "不该来"))
        self.assertEqual(verdict, 1)
        self.assertRegex(reason, r"\[记忆:\d+s前\]")
        self.assertAlmostEqual(g.last_age, 500, delta=2, msg="年龄没给出来")

    def test_age_also_works_on_file_mem(self):
        """文件缓存同样带年龄（跨进程复用也知道这结果多老）。"""
        path = os.path.join(_TMP_LOG_DIR, "age.json")
        g = Ganglion(path=path)
        g.check("文件缓存", processor=lambda c: (1, "判过"))
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        k = _make_key("文件缓存")
        data[k][2] = time.time() - 300
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        g2 = Ganglion(path=path)
        verdict, reason = g2.check("文件缓存", processor=lambda c: (0, "不该来"))
        self.assertEqual(verdict, 1)
        self.assertRegex(reason, r"\[记忆:\d+s前\]")
        self.assertAlmostEqual(g2.last_age, 300, delta=2, msg="跨实例复用没带年龄")

    def test_ttl_zero_forever_still_reports_age(self):
        """ttl=0 永不过期，年龄照给。"""
        mem = {}
        g = Ganglion(mem=mem, ttl=0)
        g.check("永生", processor=lambda c: (1, "v1"))
        key = _make_key("永生")
        v, r, t = mem[key]
        mem[key] = (v, r, time.time() - 999999)
        g.check("永生", processor=lambda c: (0, "拒"))
        self.assertGreater(g.last_age, 999999)


# ══════════ v0 的 19 条语义（原样移植到 v1.1） ══════════

class TestKey(unittest.TestCase):
    """键的归一化：同一句话不同写法应当命中同一条记录。"""

    def test_strip_noise(self):
        self.assertEqual(_make_key("你好，世界！"), _make_key("你好世界"))

    def test_mention_stripped(self):
        self.assertEqual(_make_key("@Alice 转账"), _make_key("Alice 转账"))

    def test_truncate_64(self):
        a = "x" * 100
        b = "x" * 64 + "截然不同的尾巴"
        self.assertEqual(_make_key(a), _make_key(b))

    def test_hash_16(self):
        k = _make_key("hello")
        self.assertEqual(len(k), 16)
        self.assertTrue(all(c in "0123456789abcdef" for c in k))


class TestMemo(unittest.TestCase):
    """记忆：命中、过期、无记忆三种状态。"""

    def test_hit_returns_memory(self):
        mem = {}
        g = Ganglion(mem=mem)
        g.check("同一条", processor=lambda c: (1, "通过"))
        verdict, reason = g.check("同一条", processor=lambda c: (0, "不该重判"))
        self.assertEqual(verdict, 1)
        self.assertIn("[记忆:", reason)

    def test_miss_runs_processor(self):
        g = Ganglion(mem={})
        verdict, reason = g.check("新内容", processor=lambda c: (1, "放行"))
        self.assertEqual(verdict, 1)
        self.assertEqual(reason, "放行")

    def test_ttl_expired_reruns(self):
        mem = {}
        g = Ganglion(mem=mem, ttl=1)
        g.check("旧条目", processor=lambda c: (1, "第一版"))
        key = _make_key("旧条目")
        v, r, t = mem[key]
        mem[key] = (v, r, t - 100)
        verdict, reason = g.check("旧条目", processor=lambda c: (0, "第二版"))
        self.assertEqual(verdict, 0)
        self.assertEqual(reason, "第二版")

    def test_ttl_zero_forever(self):
        mem = {}
        g = Ganglion(mem=mem, ttl=0)
        g.check("永生", processor=lambda c: (1, "v1"))
        key = _make_key("永生")
        v, r, t = mem[key]
        mem[key] = (v, r, t - 999999)
        verdict, reason = g.check("永生", processor=lambda c: (0, "拒"))
        self.assertEqual(verdict, 1)
        self.assertIn("[记忆:", reason)

    def test_no_mem_always_runs(self):
        calls = []
        g = Ganglion()
        g.check("x", processor=lambda c: (calls.append(1), (1, "a"))[1])
        g.check("x", processor=lambda c: (calls.append(1), (1, "a"))[1])
        self.assertEqual(len(calls), 2)


class TestFailOpen(unittest.TestCase):
    """fail-open：检测失败不阻塞，且不落缓存。"""

    def test_exception_returns_none(self):
        g = Ganglion(mem={})
        verdict, reason = g.check("会炸的", processor=lambda c: 1 / 0)
        self.assertIsNone(verdict)
        self.assertIn("检测异常", reason)

    def test_exception_not_written(self):
        mem = {}
        g = Ganglion(mem=mem)
        g.check("会炸的", processor=lambda c: 1 / 0)
        self.assertEqual(len(mem), 0, "降级判决被写进缓存了！")

    def test_exception_not_written_after_hit(self):
        mem = {}
        g = Ganglion(mem=mem)
        g.check("稳定", processor=lambda c: (1, "通过"))
        before = dict(mem)
        g.check("稳定", processor=lambda c: 1 / 0)
        self.assertEqual(mem, before)


class TestScan(unittest.TestCase):
    """批量扫描。"""

    def test_scan_returns_all(self):
        g = Ganglion(mem={})
        results = g.scan(["a", "b", "c"], processor=lambda c: (1, c))
        self.assertEqual(len(results), 3)
        for key, verdict, reason in results:
            self.assertEqual(verdict, 1)
            self.assertEqual(len(key), 16)

    def test_scan_reuses_memory(self):
        calls = []

        def proc(c):
            calls.append(c)
            return (1, "ok")

        g = Ganglion(mem={})
        g.scan(["a", "b"], processor=proc)
        self.assertEqual(len(calls), 2)
        g.scan(["a", "b"], processor=proc)
        self.assertEqual(len(calls), 2, "重复扫描没走记忆！")


class TestHealthCheck(unittest.TestCase):
    """健康检查型神经节。"""

    def test_probe_ok(self):
        g = Ganglion.health_check("DB", lambda: (True, "通"))
        verdict, reason = g.check("任意")
        self.assertEqual(verdict, 1)
        self.assertIn("DB", reason)

    def test_probe_down(self):
        g = Ganglion.health_check("DB", lambda: (False, "挂了"))
        verdict, reason = g.check("任意")
        self.assertEqual(verdict, 0)

    def test_probe_error(self):
        def bad():
            raise RuntimeError("boom")

        g = Ganglion.health_check("DB", bad)
        verdict, reason = g.check("任意")
        self.assertEqual(verdict, 0)
        self.assertIn("检查异常", reason)


class TestVerdict(unittest.TestCase):
    """三值语义：1 / 0 / None。"""

    def test_three_values(self):
        g = Ganglion(mem={})
        self.assertEqual(g.check("a", processor=lambda c: (1, "ok"))[0], 1)
        self.assertEqual(g.check("b", processor=lambda c: (0, "no"))[0], 0)
        self.assertIsNone(g.check("c", processor=lambda c: (None, "待定"))[0])

    def test_none_also_recorded(self):
        mem = {}
        g = Ganglion(mem=mem)
        g.check("待定的", processor=lambda c: (None, "待定"))
        self.assertEqual(len(mem), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""神经节 v0 回归测试 —— 零依赖，`python -m unittest` 直接跑。

覆盖 TRAE 独立测试（2026-10-30）里最硬的几条结论，
其中「异常路径不落账」是它判定「能用」与「可用」的分界线。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ganglion import Ganglion, _make_key


class TestKey(unittest.TestCase):
    """键的归一化：同一句话不同写法应当命中同一条账。"""

    def test_strip_noise(self):
        """@名、标点、空白剥掉后同句同键。"""
        self.assertEqual(_make_key("你好，世界！"), _make_key("你好世界"))

    def test_mention_stripped(self):
        """@Alice 和 Alice 是同一个键（TRAE 反例：文档说剥得掉，实测剥不掉——此处钉住真实行为）。"""
        self.assertEqual(_make_key("@Alice 转账"), _make_key("Alice 转账"))

    def test_truncate_64(self):
        """只取前 64 字符做键，长文本不漂移。"""
        a = "x" * 100
        b = "x" * 64 + "截然不同的尾巴"
        self.assertEqual(_make_key(a), _make_key(b))

    def test_hash_16(self):
        """sha256 前 16 位十六进制。"""
        k = _make_key("hello")
        self.assertEqual(len(k), 16)
        self.assertTrue(all(c in "0123456789abcdef" for c in k))


class TestMemo(unittest.TestCase):
    """记忆：命中、过期、无记忆三种状态。"""

    def test_hit_returns_memory(self):
        """命中返回旧判决，并带 [记忆:Ns前] 前缀。"""
        mem = {}
        g = Ganglion(mem=mem)
        g.check("同一条", processor=lambda c: (1, "通过"))
        verdict, reason = g.check("同一条", processor=lambda c: (0, "不该重判"))
        self.assertEqual(verdict, 1)
        self.assertIn("[记忆:", reason)

    def test_miss_runs_processor(self):
        """没命中就跑 processor。"""
        g = Ganglion(mem={})
        verdict, reason = g.check("新内容", processor=lambda c: (1, "放行"))
        self.assertEqual(verdict, 1)
        self.assertEqual(reason, "放行")

    def test_ttl_expired_reruns(self):
        """过期后重新判。"""
        mem = {}
        g = Ganglion(mem=mem, ttl=1)
        g.check("旧账", processor=lambda c: (1, "第一版"))
        # 手动把时间戳推到过去，模拟过期
        key = _make_key("旧账")
        v, r, t = mem[key]
        mem[key] = (v, r, t - 100)
        verdict, reason = g.check("旧账", processor=lambda c: (0, "第二版"))
        self.assertEqual(verdict, 0)
        self.assertEqual(reason, "第二版")

    def test_ttl_zero_forever(self):
        """ttl=0 永不过期。"""
        mem = {}
        g = Ganglion(mem=mem, ttl=0)
        g.check("永生", processor=lambda c: (1, "v1"))
        key = _make_key("永生")
        v, r, t = mem[key]
        mem[key] = (v, r, t - 999999)
        verdict, reason = g.check("永生", processor=lambda c: (0, "v2"))
        self.assertEqual(verdict, 1)
        self.assertIn("[记忆:", reason)

    def test_no_mem_always_runs(self):
        """不传记忆 = 纯反射，每次重算。"""
        calls = []
        g = Ganglion()
        g.check("x", processor=lambda c: (calls.append(1), (1, "a"))[1])
        g.check("x", processor=lambda c: (calls.append(1), (1, "a"))[1])
        self.assertEqual(len(calls), 2)


class TestFailOpen(unittest.TestCase):
    """fail-open：检测失败不阻塞，且【不落账】。

    TRAE 原话：「瞬时故障不会把坏判决固化一小时——
    这个细节是区分能用和可用的地方。」
    """

    def test_exception_returns_none(self):
        """processor 抛异常 → 返回 None（未决），不抛给调用方。"""
        g = Ganglion(mem={})
        verdict, reason = g.check("会炸的", processor=lambda c: 1 / 0)
        self.assertIsNone(verdict)
        self.assertIn("检测异常", reason)

    def test_exception_not_written(self):
        """★ 关键：异常路径【不写】记忆——降级不冻结。"""
        mem = {}
        g = Ganglion(mem=mem)
        g.check("会炸的", processor=lambda c: 1 / 0)
        self.assertEqual(len(mem), 0, "降级判决被写进账本了！")

    def test_exception_not_written_after_hit(self):
        """已有账不受后续异常影响。"""
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
        items = ["a", "b", "c"]
        results = g.scan(items, processor=lambda c: (1, c))
        self.assertEqual(len(results), 3)
        for key, verdict, reason in results:
            self.assertEqual(verdict, 1)
            self.assertEqual(len(key), 16)

    def test_scan_reuses_memory(self):
        """扫第二遍时同内容命中，processor 不再跑。"""
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
    """健康探针型神经节。"""

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
        """探针自己炸 → 判 0（探针异常）。"""
        def bad():
            raise RuntimeError("boom")
        g = Ganglion.health_check("DB", bad)
        verdict, reason = g.check("任意")
        self.assertEqual(verdict, 0)
        self.assertIn("探针异常", reason)


class TestVerdict(unittest.TestCase):
    """三值语义：1 / 0 / None。"""

    def test_three_values(self):
        g = Ganglion(mem={})
        self.assertEqual(g.check("a", processor=lambda c: (1, "ok"))[0], 1)
        self.assertEqual(g.check("b", processor=lambda c: (0, "no"))[0], 0)
        self.assertIsNone(g.check("c", processor=lambda c: (None, "待定"))[0])

    def test_none_also_recorded(self):
        """未决(None)是正常判决，要记账；只有异常才不记。"""
        mem = {}
        g = Ganglion(mem=mem)
        g.check("待定的", processor=lambda c: (None, "待定"))
        self.assertEqual(len(mem), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

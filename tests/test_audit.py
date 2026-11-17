# -*- coding: utf-8 -*-
"""audit() 读账口测试（2.1）。

现场来自 2026-11-17 外部 issue：borrow 的 hit 行进流水后，只拿快照对流水的
读者看见「流水长了一行、链没长」，把**未重演**误报成**断链**。
audit 的职责：把「未重演 / 断链 / 坏行」分开报，且自己绝对只读。
"""
import hashlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger import Ledger  # noqa: E402

addr = lambda s: hashlib.sha256(s.encode()).hexdigest()[:16]


def _book(d):
    return os.path.join(d, "book.json"), os.path.join(d, "book.jsonl")


def _seed(snap, jp):
    L = Ledger(writer="t", ttl=3600)
    key = addr("会被借的一节")
    L.record(key, 1, "对", "fp|A")
    L.record(key, 1, "订正", "fp|A")
    L.save(snap, journal=jp)
    b = Ledger.load(snap, writer="v2-borrow", ttl=3600, journal=jp)
    b.probe(key, "fp|A", checked=True, journal=jp)
    return key


def test_unrehearsed_is_named_not_red():
    """issue 现场：裸读者（不带 journal）应看到 unabsorbed>0 + aligned=False，而不是'链坏了'。"""
    with tempfile.TemporaryDirectory() as d:
        snap, jp = _book(d)
        _seed(snap, jp)
        c = Ledger.load(snap, writer="t", ttl=3600)          # 只读快照，不重演
        a = c.audit(snap, journal=jp)
        assert a["unabsorbed_bytes"] > 0
        assert a["aligned"] is False
        assert a["bad_lines"] == 0                            # 没有坏数据，别混进红


def test_replay_aligns():
    """带 journal 载入（走重演）之后 audit 必须对齐、hits 回来。"""
    with tempfile.TemporaryDirectory() as d:
        snap, jp = _book(d)
        key = _seed(snap, jp)
        c = Ledger.load(snap, writer="t", ttl=3600, journal=jp)
        a = c.audit(snap, journal=jp)
        assert a["aligned"] is True and a["unabsorbed_bytes"] == 0
        assert c.nodes[key][-1]["hits"] == 1


def test_audit_is_readonly():
    """audit 不许推进消费位、不许写行、不许改 stat。"""
    with tempfile.TemporaryDirectory() as d:
        snap, jp = _book(d)
        _seed(snap, jp)
        c = Ledger.load(snap, writer="t", ttl=3600)
        before = (c._journal_upto, os.path.getsize(jp), dict(c.stat))
        for _ in range(3):
            c.audit(snap, journal=jp)
        assert (c._journal_upto, os.path.getsize(jp), c.stat) == before


def test_compact_semantics():
    """压实截空正本后：现存字节是新一代（未消费），留底计入 backup_bytes。"""
    with tempfile.TemporaryDirectory() as d:
        snap, jp = _book(d)
        L = Ledger(writer="t", ttl=3600)
        key = addr("压实前那节")
        L.record(key, 1, "对", "fp|A")
        L.save(snap, journal=jp)
        L.probe(key, "fp|A", checked=True, journal=jp)
        assert L.compact(snap, journal=jp) is not None
        a = L.audit(snap, journal=jp)
        assert a["unabsorbed_bytes"] == os.path.getsize(jp)   # 截空后新写的都算尾巴
        assert a["backup_bytes"] > 0                          # 留底被认出来了


def test_bad_line_separate_axis():
    """坏行走 bad_lines 轴，不冒充'未重演'也不冒充'断链'。"""
    with tempfile.TemporaryDirectory() as d:
        snap, jp = _book(d)
        _seed(snap, jp)
        with open(jp, "ab") as f:
            f.write(b"\xff\xfe not json\n")
        c = Ledger.load(snap, writer="t", ttl=3600, journal=jp)
        a = c.audit(snap, journal=jp)
        assert a["bad_lines"] >= 1
        assert a["aligned"] is True                           # 位置照样吃完，坏行另算一笔


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:
                fails += 1
                print(f"FAIL {name}: {type(e).__name__}: {e}")
    raise SystemExit(1 if fails else 0)

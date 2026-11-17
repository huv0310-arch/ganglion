# -*- coding: utf-8 -*-
"""该不该记的闸（欠账①）—— 判据接进记账口，源头拦。

钉七条：
  ① 不传 admit = 不拦：账本不知道你的重放概率，不替你决定。
  ② 拦下 = 不进链、keys() 不虚高、stat.admitted_out 看得见。
  ③ 拦下不写流水 —— 写了重演会把它带回链上，闸就白装了。
  ④ 判决照给：问的人拿到了答案，只是这一节不落盘，下次重算。
  ⑤ 订正/收口不过闸：resolve、revisit 必须留痕，拦掉就是抹账。
  ⑥ 重演不过闸：闸是写入时的决定，不是读取时的。
  ⑦ 悬位不被闸塌缩：拦的是这一节判决，不是那笔欠账。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import admission  # noqa: E402
from ledger import Ledger, LedgerGanglion  # noqa: E402

K = "k" * 16
NEVER = lambda node: False          # 判据说：一节都别记
ALWAYS = lambda node: True


def test_不传判据就是不拦():
    """① 默认全记 —— 不给 admit，账本不替调用方决定该不该记。"""
    L = Ledger(writer="t", ttl=3600)
    assert L.record(K, 1, "算出来的", "fp|A") is not None
    assert L.keys() == 1 and L.size() == 1
    assert L.stat.get("admitted_out", 0) == 0


def test_拦下不进链且地址不虚高():
    """② 拦在 setdefault 之前 —— 先建链再拦，keys() 会凭空多出一个 0 节的地址。"""
    L = Ledger(writer="t", ttl=3600, admit=NEVER)
    assert L.record(K, 1, "算出来的", "fp|A") is None
    assert L.keys() == 0, "拦下了却占了地址，账面看着变大、实则一个字没记"
    assert L.size() == 0
    assert L.stat["admitted_out"] == 1
    # 拦归拦，后面想记的那节照常落，圈号从 1 起、不留洞
    L2 = Ledger(writer="t", ttl=3600, admit=NEVER)
    L2.record("a", 1, "挡掉", "fp|A")
    L2.admit = ALWAYS
    n = L2.record("a", 1, "放行", "fp|A")
    assert n["round"] == 1, "被拦的那节不该占圈号"


def test_拦下不写流水():
    """③ append 被拦 = 一个字节都不写，否则重演会把它带回链上。"""
    with tempfile.TemporaryDirectory() as d:
        jp = os.path.join(d, "g.jsonl")
        L = Ledger(writer="t", ttl=3600, admit=NEVER)
        assert L.append(jp, K, 1, "算出来的", "fp|A") is None
        assert not os.path.exists(jp), "判据说不记，流水却落了盘"
        # 读的一方拿这本空账回来，自然也一节都没有
        back = Ledger.load(os.path.join(d, "g"), writer="r", admit=NEVER, empty_ok=True)
        assert back.size() == 0


def test_判决照给只是不落盘():
    """④ 问的人拿到了答案；不落盘 = 下次重算（这正是判据算过的账）。"""
    with tempfile.TemporaryDirectory() as d:
        jp = os.path.join(d, "g.jsonl")
        calls = {"n": 0}

        def proc(content):
            calls["n"] += 1
            return (1, "放行")

        led = Ledger(writer="w", ttl=3600, admit=NEVER)
        gate = LedgerGanglion(ledger=led, journal=jp, why_if_unknown="缺料")
        assert gate.check("这一节能放行吗", proc, ctx_fp="fp|v1") == (1, "放行")
        assert not os.path.exists(jp), "判据说不记，判决却写进了流水"
        assert led.size() == 0
        # 没落盘，下次就得重算 —— 判据说重算比存着便宜，才不存
        assert gate.check("这一节能放行吗", proc, ctx_fp="fp|v1") == (1, "放行")
        assert calls["n"] == 2, "没落盘就该重算，不然就是既没存又假装记住了"


def test_收口的判决不过闸():
    """⑤ 悬位刚 pop 掉，闸再拦判决就变成"欠账销了、判决没落"——比不收口更糟。"""
    L = Ledger(writer="t", ttl=3600, admit=NEVER)
    L.open_pending(K, need="还差料", writer="t")
    assert K in L.open
    p = L.resolve(K, 1, "续保了", journal=False, writer="t")
    assert p is not None, "收口该把悬位还给调用方"
    assert L.nodes.get(K), "收口的判决被闸拦了 —— 欠账销了、判决没落"
    assert L.nodes[K][0]["verdict"] == 1


def test_订正的判决不过闸():
    """⑤ revisit 留的是"我们动了判据"这条痕，拦掉就等于抹账。"""
    L = Ledger(writer="t", ttl=3600)
    L.record(K, 1, "原判", "fp|A")
    L.admit = NEVER                      # 判据晚到：现在起一节都不许记
    n = L.revisit(K, 1, 0, "按新判据不通过")
    assert n is not None, "订正被闸拦了 —— 翻面从此分不出是情况变了还是判据变了"
    assert L.size() == 2
    assert len(L.flips(K)) == 1, "翻面要算得出来，订正必须落"


def test_重演不过闸():
    """⑥ 闸是写入时的决定，不是读取时的：已经在盘上的账，读回来不许缩水。"""
    with tempfile.TemporaryDirectory() as d:
        snap = os.path.join(d, "g")
        w = Ledger(writer="w", ttl=3600)         # 写的时候还没装判据
        w.append(snap + ".jsonl", K, 1, "先记下来的", "fp|A")
        w.save(snap)
        r = Ledger.load(snap, writer="r", ttl=3600, admit=NEVER)   # 读的时候判据说全别记
        assert r.size() == 1, "重演被闸拦了 —— 已经落盘的账读回来会凭空少一节"


def test_拦下不塌缩悬位():
    """⑦ 拦的是这一节判决，不是那笔欠账 —— 悬位还在走，等下一回。"""
    L = Ledger(writer="t", ttl=3600, admit=NEVER)
    L.open_pending(K, need="还差料", writer="t")
    assert L.record(K, 1, "算出来的", "fp|A") is None
    assert K in L.open, "闸拦下判决的同时把悬位塌缩了，这笔欠账就没了着落"
    assert [p.key for p in L.unresolved()] == [K]


def test_判据本身算得对():
    """⑦ admission 的式子：期望收益 = 重放概率 × 重算成本 − 写入成本。"""
    assert admission.decide(0.028, 2041.0)[0] == "STORE"      # 0.028×2041 ≈ 57 > 1
    assert admission.decide(0.048, 1.0)[0] == "DROP"          # 0.048×1 ≈ 0.05 < 1
    assert admission.decide_for("tree_search")[0] == "STORE"
    assert admission.decide_for("judgment_repeat")[0] == "DROP"
    try:
        admission.gate()
    except ValueError:
        pass
    else:
        raise AssertionError("不给重放概率就该吵 —— 猜出来的数是假数")


def test_判据接到账本上():
    """判据和账本两条链，接上只有一行：Ledger(admit=admission.gate(...))。"""
    # tree_search：重放 0.028 × 重算 2041 秒 ≈ 57 秒收益 > 写入 1 秒 → 记
    keep = Ledger(writer="t", ttl=3600, admit=admission.gate("tree_search"))
    assert keep.record(K, 1, "值得记", "fp|A") is not None
    assert keep.size() == 1
    # judgment_repeat：0.048 × 1 秒 ≈ 0.05 < 1 → 不记，账本不会被养爆
    drop = Ledger(writer="t", ttl=3600, admit=admission.gate("judgment_repeat"))
    assert drop.record(K, 1, "不值得记", "fp|A") is None
    assert drop.keys() == 0
    assert drop.stat["admitted_out"] == 1
    # 按节细判自己写：闸只认返回值真假
    only_a = Ledger(writer="t", ttl=3600, admit=lambda node: node["ctx_fp"] == "fp|A")
    assert only_a.record("x", 1, "对上", "fp|A") is not None
    assert only_a.record("y", 1, "没对上", "fp|B") is None
    assert only_a.keys() == 1

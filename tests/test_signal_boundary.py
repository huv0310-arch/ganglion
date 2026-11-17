# -*- coding: utf-8 -*-
"""信号 vs 期望收益闸的边界（round 48）：两块各管各的，接不成一条线。

钉四件事：
  1. 信号只报事实 —— 事件里没有动作字段，读不出「该叫了」；
  2. 信号不碰账 —— 不进链、不改判决、不塌悬位、不动别的统计；
  3. 双螺旋 —— admission 与 ledger 互不 import，判据的尺子是「期望收益 ≤ 0 不记不叫」；
  4. 边界写在文档里 —— README 与 docs/LEDGER.md 都得有这段（后人一搜就撞上）。
"""
import ast
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import admission  # noqa: E402
from ledger import EVIDENCE, Ledger, addr  # noqa: E402

BANNED = {"action", "gain", "admit", "wake", "should", "call", "notify", "next"}


def _book_with_signals():
    """造出三圈况齐全的一本账，顺手留下调用前的账面快照。"""
    L = Ledger(writer="t", ttl=3600, now=lambda: 200.0)
    k1 = addr("悬着的那条")
    for t in (100.0, 105.0, 108.0):                    # 问满 3 圈 → 卡了
        L.open_pending(k1, why=EVIDENCE, need="差料", now=t)
    k2 = addr("翻过面的那条")
    L.record(k2, 1, "先判是", "fp|A", ts=100.0)
    L.record(k2, 0, "再判否", "fp|B", ts=105.0)
    L.probe(k2, "fp|B", checked=True, journal=False)   # 翻了 + 有人翻过 → 通了
    before = {
        "chain": {k: [n["verdict"] for n in v] for k, v in L.nodes.items()},
        "open": {k: p.tries for k, p in L.open.items()},
        "stat": dict(L.stat),
    }
    return L, before


def test_signal_events_carry_no_decision_fields():
    """① 信号是日志不是调用指令：字段里没有动作，读不出「该叫了」。"""
    L, _ = _book_with_signals()
    ev = L.signals(journal=False, now=200.0)
    assert ev, "三型信号一条都没响，这条体检等于没跑"
    fields = {f for e in ev for f in e}
    assert not (fields & BANNED), f"事件长出决策字段：{sorted(fields & BANNED)}"
    assert {e["type"] for e in ev} <= {"flip", "stuck", "converged"}


def test_signals_do_not_touch_the_book():
    """② 信号不进链、不改判决、不塌悬位、不动别的统计。"""
    L, before = _book_with_signals()
    L.signals(journal=False, now=200.0)
    assert {k: [n["verdict"] for n in v] for k, v in L.nodes.items()} == before["chain"]
    assert {k: p.tries for k, p in L.open.items()} == before["open"]
    after = dict(L.stat)
    assert {k: v for k, v in after.items() if k != "signals"} == \
        {k: v for k, v in before["stat"].items() if k != "signals"}


def test_waking_is_callers_math_not_the_ledgers():
    """③ 叫不叫醒是调用方拿信号现算的：同一把尺，≤0 当没响过（不记不叫）。"""
    # 期望收益 = 重放概率 × 重算成本 − 写入/叫醒成本
    assert admission.decide(0.0, 1000.0)[0] == "DROP"        # 0 × anything − cost ≤ 0
    assert admission.decide(0.5, 1.0, write_cost=2.0)[0] == "DROP"
    assert admission.decide(0.5, 10.0, write_cost=1.0)[0] == "STORE"
    keep = admission.gate(replay=0.01, cost=100.0, write_cost=1.0)   # 1 − 1 = 0 → 不叫
    assert keep(None) is False and keep.action == "DROP" and keep.gain <= 0


def test_two_chains_do_not_import_each_other():
    """③ 双螺旋：判据不读账本，账本不读判据 —— 谁写错，另一侧不跟着错。"""
    def imports(path):
        mods = set()
        for node in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
            if isinstance(node, ast.Import):
                mods.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module.split(".")[0])
        return mods

    assert "ledger" not in imports(os.path.join(ROOT, "admission.py"))
    assert "admission" not in imports(os.path.join(ROOT, "ledger.py"))
    # 信号侧连「判据」的字样都不许进账本的调用面：signals 的公开签名里没有 admit
    assert "admit" not in Ledger.signals.__code__.co_varnames


def test_boundary_is_written_down_in_both_docs():
    """④ 边界要写在文档里：后人搜「不记不叫」「期望收益」得撞上这道墙。"""
    for doc in (os.path.join(ROOT, "README.md"), os.path.join(ROOT, "docs", "LEDGER.md")):
        text = open(doc, encoding="utf-8").read()
        assert "不记不叫" in text, f"{doc} 没写「≤0 不记不叫」这条边界"
        flat = text.replace(" ", "").replace("　", "")
        assert "期望收益=重放概率×重算成本" in flat, f"{doc} 没写期望收益的式子"
        assert "别把信号直接接成调用" in flat, f"{doc} 没写「信号不许直接接成调用」"
    sig = (Ledger.signals.__doc__ or "").replace(" ", "")
    assert "别把信号接成调用" in sig and "不记不叫" in sig

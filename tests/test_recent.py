# -*- coding: utf-8 -*-
"""时间线查询 `L.recent(n)`（round 52）—— README「没有时间线查询」那一行关掉时补的钉子。

那一行的关的条件写死了：三件口径（查什么 / 按什么排 / 边界给谁）定下来，
**测试与规范同步补上**才算关。三件口径：

  ① 查什么   —— 所有地址上的每一节（节点自带 `key`），穿成一条时间线；
  ② 按什么排 —— 按节自带的钟 `ts` 降序（跨地址没有共同链序）；同 ts 按圈号降序、
                再按地址升序 —— 次序确定，不看字典迭代顺序；
  ③ 边界给谁 —— 墓碑不算（忘了的不在时间线上）、褪色节算（判决圈号还留着）；
                n 从最新往回数，不够就全给，n <= 0 给 []。

另钉两条：只读（不改链、不动统计、不落账）；没有钟的节排不了（别拿 0 当钟）。
规范同步在 docs/LEDGER.md §13.2（加方法不加面）与 README Known gaps 第三列。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger import Ledger, addr  # noqa: E402

A, B = addr("甲"), addr("乙")


def _book():
    """三条时间线的账：甲 100 → 200，乙 300（乙最新）。"""
    L = Ledger(writer="w", ttl=None, now=lambda: 0.0)
    L.record(A, 1, "甲第一圈", "fp", ts=100.0, ttl=60)
    L.record(B, 0, "乙第一圈", "fp", ts=300.0, ttl=60)
    L.record(A, 0, "甲第二圈", "fp", ts=200.0, ttl=60)
    return L


def test_recent_orders_by_ts_across_addresses():
    """② 按节自带的钟排：跨地址也是一条时间线，最新在前，每节带自己的地址。"""
    got = _book().recent(3)
    assert [n["ts"] for n in got] == [300.0, 200.0, 100.0]
    assert [n["key"] for n in got] == [B, A, A]
    assert [n["round"] for n in got] == [1, 2, 1]


def test_recent_ties_break_by_round_then_key():
    """同一个 ts：圈号降序 → 地址升序。次序必须确定，不看字典的迭代顺序。"""
    L = Ledger(writer="w", ttl=None, now=lambda: 0.0)
    L.record(addr("bb"), 1, "后落的", "fp", ts=50.0, ttl=60)
    L.record(A, 1, "先落的", "fp", ts=50.0, ttl=60)
    L.record(A, 0, "同一地址第二圈", "fp", ts=50.0, ttl=60)

    assert [(n["round"], n["key"]) for n in L.recent(3)] == [
        (2, A),        # 同址：圈号大的在前
        (1, A),        # 再按地址升序
        (1, addr("bb")),
    ]


def test_recent_counts_n_from_the_newest():
    """③ 数到哪一节为止：从最新往回数 n 节；不够就全给；n <= 0 给 []。"""
    L = _book()
    assert len(L.recent(3)) == 3
    assert len(L.recent(99)) == 3, "要得比账上多：全给，不报错"
    assert [n["ts"] for n in L.recent(1)] == [300.0]
    assert L.recent(0) == []
    assert L.recent(-3) == []


def test_recent_skips_tombstones_but_keeps_faded(tmp_path):
    """③ 边界：墓碑（忘）不算，褪色节（梗概）算 —— 两档遗忘在时间线上分得开。"""
    L = Ledger(writer="w", ttl=None, now=lambda: 1000.0)
    L.record(addr("会忘的"), 1, "过窗", "fp", ts=1000.0, ttl=60)   # 有钟：按钟能忘
    L.record(addr("留着的"), 0, "平淡一", "fp", ts=1001.0, ttl=None)
    L.record(addr("留着的"), 0, "平淡二", "fp", ts=1002.0, ttl=None)

    assert L.sweep_ttl(now=2000.0, archive=str(tmp_path / "gone.jsonl")) == 1
    assert L.tombstones() == 1
    assert L.prune() == 1, "平淡前圈该褪成梗概"
    assert any(n.get("demoted") for n in L.nodes[addr("留着的")])

    got = L.recent(99)
    assert all(not L.is_tombstone(n) for n in got), "忘了的不许出现在时间线上"
    assert any(n.get("demoted") for n in got), "褪成梗概的节还在这本账上，要算数"
    assert len(got) == L.size() == 2


def test_recent_skips_nodes_without_a_clock():
    """没有钟的节排不了位 —— 照 sweep_ttl 的规矩跳过，别拿 0 当钟。"""
    L = _book()
    L.nodes[addr("没钟的")] = [{"key": addr("没钟的"), "verdict": 1,
                               "round": 1, "ts": None}]

    got = L.recent(99)
    assert len(got) == 3
    assert all(n["ts"] is not None for n in got)


def test_recent_is_read_only(tmp_path):
    """只读：不改链、不动统计、不落一个字节的账（连目录都不许多出文件）。"""
    L = _book()
    before = json.dumps(L.to_dict(), sort_keys=True, ensure_ascii=False, default=str)
    stat = dict(L.stat)

    got = L.recent(2)

    assert json.dumps(L.to_dict(), sort_keys=True, ensure_ascii=False,
                      default=str) == before, "recent 动了账"
    assert L.stat == stat, "查询不许记账"
    assert os.listdir(tmp_path) == [], "查询不许落盘"
    assert len(got) == 2


def test_recent_rejects_a_non_integer_n():
    """要几节就报几节：不是整数当场 ValueError，不猜调用方的意思。"""
    L = _book()
    for bad in ("3", 2.0, None, True):
        with pytest.raises(ValueError):
            L.recent(bad)

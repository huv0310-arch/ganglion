# -*- coding: utf-8 -*-
"""Z 轴的五件：每节自带钟、收口期限、回溯、太新不借、遗忘。"""
import pytest

from ganglion import _make_key
from ledger import Ledger, LedgerBlocked, EXPIRED, TOO_FRESH


K = _make_key("某台车合规")


def test_each_round_carries_its_own_clock():
    """一本账里快细胞和慢细胞并存 —— 共用一个 ttl 就是拿肠子管神经元。"""
    now = [1000.0]
    L = Ledger(writer="t", now=lambda: now[0])
    L.record(K, 1, "年检", "fp|a", ttl=10)        # 快细胞：10 秒就过期
    L.record(K, 1, "保险", "fp|b", ttl=10_000)    # 慢细胞：一万秒还新鲜

    now[0] = 1100.0
    assert L.probe(K, "fp|b", journal=False) is not None         # 慢的还能借
    assert L.probe(K, "fp|a", journal=False) is None             # 快的已经过期
    assert L.why_not(K) == EXPIRED


def test_no_clock_anywhere_refuses_to_lend():
    L = Ledger(writer="t")
    L.record(K, 1, "没给钟", "fp|a")
    with pytest.raises(LedgerBlocked):
        L.probe(K, "fp|a", journal=False)


def test_min_age_refuses_a_too_fresh_answer():
    """刚到三秒又问一遍：那是抖动，不是结论。"""
    now = [1000.0]
    L = Ledger(writer="t", ttl=3600, now=lambda: now[0])
    L.record(K, 1, "刚判完", "fp|a")

    now[0] = 1003.0
    assert L.probe(K, "fp|a", min_age=60, journal=False) is None
    assert L.why_not(K) == TOO_FRESH
    now[0] = 1100.0
    assert L.probe(K, "fp|a", min_age=60, journal=False) is not None


def test_decide_by_marks_when_it_is_overdue():
    now = [1000.0]
    L = Ledger(writer="t", now=lambda: now[0])
    L.open_pending(K, why="evidence", need="缺车架号", decide_by=1000.0 + 60)

    assert L.due() == []
    now[0] = 1061.0
    assert len(L.due()) == 1
    p = L.resolve(K, forced=True, journal=False)                  # 到点无据，逼着收口
    assert p.forced is True
    assert L.stat["forced"] == 1
    assert L.stat["resolved"] == 1


def test_revisit_never_touches_the_old_round():
    """判据变了 ≠ 情况变了。旧节一个字不许改。"""
    L = Ledger(writer="t")
    first = L.record(K, 1, "按旧判据通过", "fp|a")
    L.revisit(K, first["round"], 0, "按新判据不通过", value_score=0.8)

    chain = L.nodes[K]
    assert chain[0]["verdict"] == 1                # 旧节原样
    assert chain[1]["revisit_of"] == 1
    assert chain[1]["mirror_ts"] == first["ts"]
    flips = L.flips(K)
    assert len(flips) == 1
    assert flips[0]["kind"] == "判据变了"


def test_prune_keeps_history_and_fades_the_bland():
    """三档遗忘（2026-11-04 定案）：平淡圈褪成梗概，判决和圈号留住；墓碑只由 sweep_ttl 立。"""
    L = Ledger(writer="t")
    for _ in range(5):
        L.record(K, 0, "平淡的一圈", "fp|a")       # 判过、没人翻、不参与翻面
    L.record(K, 1, "终于疼了", "fp|a")            # 第 6 圈：翻面
    before_rounds = L.by_round(K)

    killed = L.prune()
    assert killed == 4                             # 只褪平淡的那几圈
    assert L.by_round(K) == before_rounds          # 圈号一个字没乱
    assert L.tombstones() == 0                     # prune 只降级：墓碑是 sweep_ttl 的活
    faded = [n for n in L.nodes[K] if n.get("demoted")]
    assert len(faded) == 4
    assert all(n["verdict"] == 0 for n in faded)   # 梗概的底线：判过什么留住
    assert all(n["ctx_fp"] == "" for n in faded)   # 细节褪掉
    assert L.by_verdict(K) == 1                    # 当前判决还在
    assert len(L.flips(K)) == 1                    # 翻面还在（褪了也不算翻面消失）


def test_prune_never_eats_a_read_round():
    L = Ledger(writer="t", ttl=3600, now=lambda: 1000.0)
    L.record(K, 0, "判过", "fp|a")
    L.record(K, 0, "判过又被翻过", "fp|a")
    L.probe(K, "fp|a", journal=False)                              # 翻最新那节一次
    assert L.prune() == 1                           # 没人翻的那圈褪成梗概
    assert L.nodes[K][0].get("demoted") is True     # 褪的是没人翻的第一圈
    assert L.nodes[K][1].get("demoted") is not True # 被翻过的不许褪
    assert L.nodes[K][1]["hits"] == 1
    assert L.size() == 2                            # 褪不等于删：两节都还在格上
    assert L.by_round(K) == [1, 2]                  # 圈号一个字没乱

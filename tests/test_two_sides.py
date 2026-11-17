# -*- coding: utf-8 -*-
"""结算第一刀（round 46）：两行结构 `expect()` / `actual()` + 差值 `gap()`。

钉四件事：
  1. 两笔都留痕、差值回流（预计 − 实际）；
  2. 缺笔如实报 None —— 缺笔不是 0 分；
  3. 只收数字、journal 必填、要落账必须署名（和别的口子同一把尺）；
  4. 流水是正本、快照也带这两行：换个人读、落盘读，都算得出来。
"""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ledger import Ledger, addr  # noqa: E402

NOW = 200.0


def book(tmp_path=None):
    jp = None if tmp_path is None else str(tmp_path / "two.jsonl")
    return Ledger(writer="t", ttl=3600, now=lambda: NOW), jp


# ── 一、两行结构 ────────────────────────────────────────────────

def test_two_lines_and_gap(tmp_path):
    """预计一笔、实际一笔，差值当场回流。"""
    L, jp = book(tmp_path)
    k = addr("预计与实际")
    L.expect(k, 0.7, journal=jp)
    L.actual(k, 0.5, journal=jp)
    assert L.gap(k) == pytest.approx(0.2)


def test_missing_side_is_none_not_zero(tmp_path):
    """缺笔不是 0 分：还没结算 ≠ 正好猜中，混了就等于替你编了一个「猜中」。"""
    L, jp = book(tmp_path)
    k = addr("只有预计")
    L.expect(k, 0.9, journal=jp)
    assert L.gap(k) is None
    assert L.gap(addr("两笔都没有")) is None


def test_gap_without_key_lists_only_written_sides(tmp_path):
    """不给 key：只列写过笔的地址，缺的那一侧照样是 None。"""
    L, jp = book(tmp_path)
    a, b = addr("甲"), addr("乙")
    L.expect(a, 1.0, journal=jp)
    L.expect(b, 0.4, journal=jp)
    L.actual(b, 0.6, journal=jp)
    gaps = L.gap()
    assert set(gaps) == {a, b}
    assert gaps[a] is None
    assert gaps[b] == pytest.approx(-0.2)


def test_latest_side_wins_in_memory_full_history_in_journal(tmp_path):
    """同一地址再写一笔 = 覆盖这一侧的旧笔（最新算数）；流水里每一笔都留着。"""
    L, jp = book(tmp_path)
    k = addr("改过预计")
    L.expect(k, 0.5, journal=jp)
    L.expect(k, 0.8, journal=jp)
    L.actual(k, 0.6, journal=jp)
    assert L.gap(k) == pytest.approx(0.2)
    with open(jp, encoding="utf-8") as f:
        rows = [json.loads(x) for x in f.read().splitlines() if x.strip()]
    assert [r["value"] for r in rows if r["t"] == "expect"] == [0.5, 0.8]


def test_two_lines_do_not_touch_the_ledger(tmp_path):
    """两笔不是判决：不进链、不占圈号、不塌悬位。"""
    L, jp = book(tmp_path)
    k = addr("只写两笔")
    L.expect(k, 0.5, journal=jp)
    L.actual(k, 0.6, journal=jp)
    assert L.nodes == {}
    assert L.open == {}
    assert L.by_verdict(k) is None


# ── 二、同一把尺：数字、地址、journal、署名 ─────────────────────

def test_only_numbers_are_accepted(tmp_path):
    """差值才算得出来：话不是数。"""
    L, jp = book(tmp_path)
    k = addr("把话当数")
    for bad in ("大概", True, None, [1]):
        with pytest.raises(ValueError):
            L.expect(k, bad, journal=jp)
    assert L.gap(k) is None


def test_key_must_be_an_address(tmp_path):
    L, jp = book(tmp_path)
    with pytest.raises(ValueError):
        L.expect("", 0.5, journal=jp)
    with pytest.raises(ValueError):
        L.actual(None, 0.5, journal=jp)


def test_journal_is_required(tmp_path):
    """不给 journal 当场报错 —— 默认不落账，换个人读回来就少一笔。"""
    L, _ = book()
    k = addr("没给流水")
    with pytest.raises(ValueError):
        L.expect(k, 0.5, journal=None)
    with pytest.raises(ValueError):
        L.actual(k, 0.5, journal="")
    with pytest.raises(TypeError):            # 必填关键字参数，缺了是 TypeError
        L.expect(k, 0.5)


def test_writer_is_required(tmp_path):
    """要落账就必须署名：没写者的两笔，出事时谁都能说不是我。"""
    jp = str(tmp_path / "bare.jsonl")
    bare = Ledger(now=lambda: NOW)
    k = addr("没署名的两笔")
    with pytest.raises(ValueError):
        bare.expect(k, 0.5, journal=jp)
    with pytest.raises(ValueError):
        bare.actual(k, 0.6, journal=jp)


# ── 三、正本与快照都读得回来 ────────────────────────────────────

def test_journal_is_the_source_of_truth(tmp_path):
    """换一个人、只重演流水，差值也算得出来（流水是正本）。"""
    L, jp = book(tmp_path)
    k = addr("预计与实际")
    L.expect(k, 0.7, journal=jp)
    L.actual(k, 0.5, journal=jp)
    fresh = Ledger(writer="t", ttl=3600)
    fresh.replay(jp)
    assert fresh.gap(k) == pytest.approx(0.2)
    assert fresh.nodes == {}


def test_snapshot_roundtrip(tmp_path):
    """落盘读回来不丢这两行（快照也带）。"""
    L, jp = book(tmp_path)
    p = str(tmp_path / "book.json")
    k = addr("预计与实际")
    L.expect(k, 0.7, journal=jp)
    L.actual(k, 0.5, journal=jp)
    L.save(p)
    back = Ledger.load(p, ttl=3600)
    assert back.gap(k) == pytest.approx(0.2)


def test_old_snapshot_without_the_column_still_loads(tmp_path):
    """老快照没有这一格 = 还没结过算，不是坏账。"""
    L, jp = book(tmp_path)
    L.record(addr("老账"), 1, "判过", "fp|A", ts=190.0)
    p = str(tmp_path / "old.json")
    L.save(p)
    with open(p, encoding="utf-8") as f:
        raw = json.load(f)
    raw.pop("sides", None)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False)
    back = Ledger.load(p, ttl=3600)
    assert back.gap() == {}
    assert len(back.nodes) == 1


def test_merge_keeps_the_later_side(tmp_path):
    """存盘前发现盘上被别人改过：同一侧按 ts 比，后写的算数，谁也不盖谁。"""
    p = str(tmp_path / "book.json")
    k = addr("两边都写过")
    disk = Ledger(writer="t", ttl=3600, now=lambda: NOW + 60.0)
    disk.expect(k, 0.9, journal=False)
    disk.save(p)
    # 我手上是更早的一笔：并进来时必须让位，旧笔不许盖新笔
    mine = Ledger(writer="t", ttl=3600, now=lambda: NOW)
    mine.expect(k, 0.5, journal=False)
    mine._merge_from(Ledger.load(p, ttl=3600))
    assert mine.sides[k]["expect"]["value"] == 0.9
    # 反过来：我手上的更新，盘上那版也不许盖掉我
    newer = Ledger(writer="t", ttl=3600, now=lambda: NOW + 120.0)
    newer.expect(k, 0.7, journal=False)
    newer._merge_from(Ledger.load(p, ttl=3600))
    assert newer.sides[k]["expect"]["value"] == 0.7

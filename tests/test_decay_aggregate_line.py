# -*- coding: utf-8 -*-
"""round 50 合体用例：多笔回填 → 衰减加权 → 情形聚合，一条线钉死。

round 48 给了「情形内折分、情形间边缘化」，round 49 给了 `decay()` 时间衰减，
两条各自有测试；本文件把它们**串成一条线**跑完，钉住的是接起来之后的四件事：

  1. 多笔回填（`expect`/`actual` 落两行 → `gap()` 读回差值 → 年龄从**账上的钟**
     算，不从调用方的脑子里算）→ 衰减加权 → 情形聚合 → 一个 `value_score`，
     中间数与手算逐位对得上（手算不借工具的秤，权重公式写死在测试里）；
  2. 组合律：`aggregate()` 等于「先 `by_context()` 再按权重占比边缘化」——
     两步就是条件概率表那两步，不是又长出第三个公式；
  3. 新回填说话重、顺序不进聚合：把最新那笔回填改好，分涨得多；把回填顺序
     打乱，数一个字不变（求和可交换，新旧之别全在权重里）；
  4. 落盘往返：两行写进流水、存快照、换本读回来，差值与年龄原样，聚合数一致
     ——「账上读得出来的」才算数，内存里算出来的不算。

外加两条护栏：缺笔（只记了预计没记实际）不进这条线、不占 P(fp)；
不给 age 就退化成 round 48 的等权/笔数占比（老行为一个字不改）。
工具层仍然不碰账：本文件里 `tools.gap_value` 一行都没 import ledger。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tools.gap_value as gv  # noqa: E402
from ledger import Ledger, addr  # noqa: E402

HALF = gv.DEFAULT_HALF_LIFE          # 604800 秒 = 一周（带说明的默认值）
NOW = 1_000_000.0                   # 固定的“现在”，把时间钉死才好手算

# 多笔回填：（情形指纹, 预计, 实际, 判决那一刻的 ts）
ROWS = [
    ("fp|A", 0.7, 0.5, 100_000.0),    # A · 老：距今 900000（一个多半衰期）
    ("fp|A", 0.9, 0.9, 900_000.0),    # A · 中：距今 100000
    ("fp|A", 1.0, 0.6, 999_000.0),    # A · 新：距今 1000（gap 0.4，拖后腿的那笔）
    ("fp|B", 0.4, 0.6, 500_000.0),    # B · 老：距今 500000（gap −0.2）
    ("fp|B", 0.5, 0.5, 999_500.0),    # B · 新：距今 500（正好猜中）
]


def w(age):
    """手算用的衰减权重 —— 公式写死在这里，不借 gv.decay 的秤。"""
    return 0.5 ** (age / HALF)


def sc(gap):
    """手算用的折分（默认 scale=1.0）：1 / (1 + |gap|)。"""
    return 1.0 / (1.0 + abs(gap))


def backfill(rows=ROWS, *, journal=False, snap=None):
    """一条线的前半段：账上记两行 → 读回差值 → 年龄从账上的钟算出来。

    `journal` 是两行走的流水（`False` = 明说不落，单进程），`snap` 是另起的
    快照文件 —— 流水与快照必须是两个文件，同一份文件装不下两种格式。
    返回 `[(fp, gap, age), …]`（喂给 `aggregate()` 的逐笔形状）和那本账。
    """
    L = Ledger(writer="t", ttl=3600, now=lambda: NOW)
    out = []
    for i, (fp, exp, act, ts) in enumerate(rows):
        k = addr(f"合体示例{i}")
        L.record(k, 1, "判过", fp, ts=ts)
        L.expect(k, exp, journal=journal)
        L.actual(k, act, journal=journal)
        # 年龄从**账上的节点 ts** 算：钟在账上，不在调用方的脑子里
        ts_on_node = L.nodes[k][-1]["ts"]
        out.append((fp, L.gap(k), NOW - ts_on_node))
    if snap:
        L.save(snap)
    return out, L


def manual_value(rows):
    """手算整条线：情形内 Σ w·score / Σ w，情形间按权重占比边缘化。"""
    acc = {}
    for fp, gap, age in rows:
        acc.setdefault(fp, []).append((sc(gap), w(age)))
    per, weights = {}, {}
    for fp, items in acc.items():
        sw = sum(a for _, a in items)
        per[fp] = sum(s * a for s, a in items) / sw
        weights[fp] = sw
    total = sum(weights.values())
    return sum(per[fp] * weights[fp] / total for fp in weights), per, weights


# ── 一、一条线跑通，中间数与手算逐位对上 ──────────────────────────

def test_backfill_decay_aggregate_one_line():
    """多笔回填 → 衰减加权 → 情形聚合：整条线的数手算对得上。"""
    rows, _ = backfill()
    assert [fp for fp, _, _ in rows] == [r[0] for r in ROWS]      # 分桶是账上的指纹
    # 差值回流（0.7−0.5=0.2 这类小数带浮点尾巴，用 approx 量）
    assert [gap for _, gap, _ in rows] == pytest.approx([0.2, 0.0, 0.4, -0.2, 0.0])
    assert [age for _, _, age in rows] == [900_000, 100_000, 1_000, 500_000, 500]

    got = gv.aggregate(rows)
    want, per, weights = manual_value(rows)
    assert got == pytest.approx(want)

    # 情形内那一层（中间的条件表）也逐位对得上 —— 衰减真在情形内起了作用
    assert gv.by_context(rows) == pytest.approx(per)
    # 衰减确实改变了数：不衰减（等权）是另一个数
    assert got != pytest.approx(gv.aggregate([(fp, gap) for fp, gap, _ in rows]))


def test_aggregate_is_by_context_then_weight_share():
    """组合律：aggregate = Σ P(fp) × by_context[fp]，P 是权重占比不是笔数。"""
    rows, _ = backfill()
    per = gv.by_context(rows)
    weights = {}
    for fp, _, age in rows:
        weights[fp] = weights.get(fp, 0.0) + w(age)
    total = sum(weights.values())
    manual = sum(per[fp] * weights[fp] / total for fp in weights)
    assert gv.aggregate(rows) == pytest.approx(manual)
    # 权重占比 ≠ 笔数占比：A 三笔、B 两笔，但衰减后 P 不是 3:2
    assert weights["fp|A"] / total != pytest.approx(3 / 5)


# ── 二、新回填说话重；顺序不进聚合 ────────────────────────────────

def test_newer_backfill_moves_the_number_more():
    """同样改一笔，改最新的那笔分动得多 —— 「新回填说话重」是权重给的。"""
    rows, _ = backfill()
    base = gv.aggregate(rows)

    def swap(newest_first_row, gap):
        rs = list(rows)
        fp, _, age = rs[newest_first_row]
        rs[newest_first_row] = (fp, gap, age)
        return rs

    # A 最新那笔（age 1000）和 A 最老那笔（age 900000）都从 0.4 改成 0.0
    newer = gv.aggregate(swap(2, 0.0))      # 新回填：把拖后腿的那笔变准
    older = gv.aggregate(swap(0, 0.0))      # 老回填：同样改一笔
    assert newer > base and older > base    # 改哪笔都变准（分数单调）
    assert newer - base > older - base      # 新的那笔动得更多（权重 0.999 vs 0.36）


def test_backfill_order_does_not_enter_the_aggregate():
    """回填顺序不进聚合：把整条线倒着喂，数一个字不变。"""
    rows, _ = backfill()
    base = gv.aggregate(rows)
    assert gv.aggregate(list(reversed(rows))) == pytest.approx(base)
    assert gv.aggregate([rows[i] for i in (2, 4, 0, 3, 1)]) == pytest.approx(base)
    # 情形内也一样：倒序喂条件层，条件分不变
    assert gv.by_context(list(reversed(rows))) == pytest.approx(gv.by_context(rows))


# ── 三、落盘往返：读得出来的才算数 ────────────────────────────────

def test_line_survives_a_disk_roundtrip(tmp_path):
    """两行写进流水、存快照、换本读回来：差值、年龄、聚合数原样。"""
    jp = str(tmp_path / "settle.jsonl")
    snap = str(tmp_path / "book.jsonl")
    rows, L = backfill(journal=jp, snap=snap)

    raw = open(jp, encoding="utf-8").read()
    assert raw.count('"t": "expect"') == len(ROWS)     # 流水是正本，每笔都留着
    assert raw.count('"t": "actual"') == len(ROWS)

    back = Ledger.load(snap, ttl=3600)
    again = []
    for i, (fp, _, _) in enumerate(rows):
        k = addr(f"合体示例{i}")
        assert back.gap(k) == pytest.approx(L.gap(k))          # 差值读得回来
        ts_on_node = back.nodes[k][-1]["ts"]                   # 年龄的钟也在账上
        again.append((fp, back.gap(k), NOW - ts_on_node))
    assert again == pytest.approx(rows)                        # 年龄原样
    assert gv.aggregate(again) == pytest.approx(gv.aggregate(rows))   # 聚合数一致


# ── 四、护栏：缺笔不进线；不给 age 退化成 round 48 ────────────────

def test_missing_side_stays_out_of_the_line():
    """只记了预计没记实际：gap=None → 不进分母、不占 P，全缺时整条线是 None。"""
    L = Ledger(writer="t", ttl=3600, now=lambda: NOW)
    k = addr("只记了预计的合体节")
    L.record(k, 1, "判过", "fp|A", ts=900_000.0)
    L.expect(k, 0.9, journal=False)
    rows = [("fp|A", L.gap(k), NOW - 900_000.0), ("fp|B", 0.0, 500.0)]
    assert rows[0][1] is None
    # 缺的那笔让 fp|A 整个让位：线上的数只剩 B 的那一笔
    assert gv.aggregate(rows) == pytest.approx(1.0)
    assert gv.by_context(rows)["fp|A"] is None
    assert gv.aggregate([("fp|A", None, 0.0)]) is None


def test_no_age_keeps_round48_equal_weight_behaviour():
    """护栏：不给 age = 不衰减，同一批差值退化成等权/笔数占比（老行为一字不改）。"""
    rows, _ = backfill()
    plain = [(fp, gap) for fp, gap, _ in rows]
    per = manual_value(rows)[1]
    scores = {}
    for fp, gap, _ in rows:
        scores.setdefault(fp, []).append(sc(gap))
    want_per = {fp: sum(v) / len(v) for fp, v in scores.items()}
    assert gv.by_context(plain) == pytest.approx(want_per)
    total = sum(len(v) for v in scores.values())
    want = sum(want_per[fp] * len(scores[fp]) / total for fp in scores)
    assert gv.aggregate(plain) == pytest.approx(want)
    assert want != pytest.approx(manual_value(rows)[0])   # 两条口径确实不是一回事


# ── 五、三件套空格：半衰期与 scale 仍是调用方的 ────────────────────

def test_half_life_and_scale_stay_open():
    """半衰期多长、腰斩线多宽，都在调用方手里 —— 传了就改，不传是默认。"""
    rows, _ = backfill()
    base = gv.aggregate(rows)
    assert gv.aggregate(rows, half_life=86_400) != pytest.approx(base)   # 衰得快：老笔更轻
    assert gv.aggregate(rows, scale=2.0) != pytest.approx(base)          # 腰斩线宽：分整体抬高
    assert "half_life" in gv.aggregate.__doc__                           # 默认值不是暗的

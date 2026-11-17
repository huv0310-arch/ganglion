# -*- coding: utf-8 -*-
"""gap() 回流成数（round 47）+ 多笔按 ctx_fp 聚合（round 48）+ 回填时机与时间衰减（round 49）：可选工具层的示例口径。

钉六件事：
  1. 公式对得上数 —— gap=0 → 1.0、差 scale腰斩、单调不归零、高估低估一样扣；
  2. 缺笔不是 0 分 —— gap() 报 None，折算原样传 None，绝不编一个「正好猜中」；
  3. 三件套铁律 —— scale 是带说明的默认值（空格 + 默认值 + 说明缺一不可）；
  4. 不硬绑主链 —— 工具层不 import ledger、不回填；接线是调用方的示例，不是默认行为；
  5. 聚合按条件概率表走 —— 情形内逐笔折分再平均、情形间按笔数占比边缘化，
     缺笔不投票、先折后平均不许让 +/− 在差值层抵消、全缺返回 None；
  6. 回填时机与时间衰减 —— 顺序不进聚合、权重进（decay 默认半衰期一周，age 与
     half_life 同单位，没给 age 就等权 = round 48 行为），坏 age/坏半衰期当场拒。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tools.gap_value as gv  # noqa: E402
from ledger import Ledger, addr  # noqa: E402


# ── 一、公式对得上数 ────────────────────────────────────────────

def test_perfect_prediction_is_full_score():
    """正好猜中（gap=0）→ 1.0：满分不靠默认值蒙出来，是算出来的。"""
    assert gv.score(0) == pytest.approx(1.0)
    assert gv.score(0.0) == pytest.approx(1.0)


def test_scale_gap_halves_the_score():
    """差满一个 scale = 腰斩（0.5）—— 这就是 scale 的定义。"""
    assert gv.score(1.0) == pytest.approx(0.5)
    assert gv.score(2.0, scale=2.0) == pytest.approx(0.5)
    assert gv.score(1.0, scale=2.0) == pytest.approx(2.0 / 3.0)


def test_monotone_and_never_zero():
    """越准分越高，差得再远也只是接近 0 —— 「不准」不等于「验过不值」。"""
    scores = [gv.score(x) for x in (0.1, 0.5, 1.0, 5.0, 100.0)]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] > scores[-1] > 0
    assert all(0 < s <= 1 for s in scores)


def test_direction_is_blind():
    """方向不偏不倚：高估和低估一样扣（|gap|）。要偏袒某侧，使用者在外面改。"""
    assert gv.score(0.3) == pytest.approx(gv.score(-0.3))


# ── 二、缺笔不是 0 分 ───────────────────────────────────────────

def test_missing_side_passes_none_through():
    """gap() 报 None（缺笔），折算原样传 None —— 绝不替你编一个数。"""
    assert gv.score(None) is None


def test_non_number_is_refused():
    """话不是数，和账本两行同一把尺：非数字当场 ValueError。"""
    with pytest.raises(ValueError):
        gv.score("大概")
    with pytest.raises(ValueError):
        gv.score(True)


def test_bad_scale_is_refused():
    """scale 是取舍不是装饰：0 或负数当场拒（腰斩线必须是正数）。"""
    with pytest.raises(ValueError):
        gv.score(0.1, scale=0)
    with pytest.raises(ValueError):
        gv.score(0.1, scale=-2)


# ── 三、三件套：空格、默认值、说明 ──────────────────────────────

def test_scale_has_a_default_and_stays_a_gap():
    """默认值在、空格还在：不传 scale 照算，传了照改 —— 不是只有答案。"""
    assert gv.score(1.0) == pytest.approx(0.5)          # 默认 scale=1.0
    assert gv.score(1.0, scale=4.0) == pytest.approx(4.0 / 5.0)
    assert "scale" in gv.score.__doc__                  # 说明写在 docstring 里


# ── 四、不硬绑主链；接线是调用方的示例 ──────────────────────────

def test_tool_does_not_import_the_ledger():
    """工具层不硬绑：真 import 里不许出现 ledger —— 接不接线是调用方的事。

    用 ast 数 import（不数散文）：说明层提到 ledger 不算绑，真把它拉进
    依赖图才算。
    """
    import ast
    src = open(os.path.join(ROOT, "tools", "gap_value.py"), encoding="utf-8").read()
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    assert "ledger" not in mods


def test_optional_wiring_end_to_end(tmp_path):
    """示例接线跑得通（但发生在调用方，不在工具里）：差值 → 折数 → 回填读得出。"""
    L = Ledger(writer="t", ttl=3600, now=lambda: 200.0)
    k = addr("接线示例")
    L.record(k, 1, "判过", "fp|A", ts=100.0)
    L.expect(k, 0.7, journal=False)
    L.actual(k, 0.5, journal=False)
    s = gv.score(L.gap(k))                  # 0.7 − 0.5 = 0.2 → 1/1.2
    assert s == pytest.approx(1.0 / 1.2)
    L.backfill_value(k, 1, s)               # 回填哪一圈、回不回填：调用方定
    assert L.by_value(k) == pytest.approx(1.0 / 1.2)


def test_optional_wiring_missing_side_backfills_nothing(tmp_path):
    """缺一笔：折算是 None，回填那步自然不发生 —— value_score 仍是未回填的 None。"""
    L = Ledger(writer="t", ttl=3600, now=lambda: 200.0)
    k = addr("只记了预计的")
    L.record(k, 1, "判过", "fp|A", ts=100.0)
    L.expect(k, 0.9, journal=False)
    s = gv.score(L.gap(k))
    assert s is None
    if s is not None:                       # 缺笔不许走回填那一步
        L.backfill_value(k, 1, s)
    assert L.by_value(k) is None            # 未回填 ≠ 0 分


# ── 五、多笔按 ctx_fp 聚合成一个数（round 48 示例） ──────────────

def test_by_context_folds_each_bucket_separately():
    """条件层：一个情形一个分，情形内多笔等权平均 —— 分桶是 ctx_fp 说了算。"""
    per = gv.by_context({"fp|A": [0.0, 1.0], "fp|B": [0.0]})
    assert per["fp|A"] == pytest.approx((1.0 + 0.5) / 2)
    assert per["fp|B"] == pytest.approx(1.0)


def test_aggregate_matches_conditional_probability_table():
    """边缘层 value = Σ P(fp) × (score|fp)：笔数占比就是 P(fp)，能手算对上。"""
    # fp|A 三笔全中 → 1.0；fp|B 一笔差满 scale → 0.5；P(A)=3/4, P(B)=1/4
    v = gv.aggregate({"fp|A": [0.0, 0.0, 0.0], "fp|B": [1.0]})
    assert v == pytest.approx(3 / 4 * 1.0 + 1 / 4 * 0.5)
    # 一个笔数一个样：两边各一笔时是简单平均
    assert gv.aggregate({"fp|A": [0.0], "fp|B": [1.0]}) == pytest.approx(0.75)


def test_aggregate_is_a_single_score_of_one_bucket():
    """单情形单笔：聚合必须退化成单笔折分 —— 不是又发明了第二个公式。"""
    assert gv.aggregate({"fp|A": [1.0]}) == pytest.approx(gv.score(1.0))
    assert gv.aggregate([("fp|A", 1.0)]) == pytest.approx(gv.score(1.0))


def test_over_and_under_do_not_cancel_across_scores():
    """先折后平均，不先平均 gap：+1 和 −1 在差值层会抵消成 0（假的满分）。"""
    per = gv.by_context({"fp|A": [1.0, -1.0]})
    assert per["fp|A"] == pytest.approx(0.5)      # 两笔各腰斩 → 0.5
    assert gv.aggregate({"fp|A": [1.0, -1.0]}) == pytest.approx(0.5)


def test_missing_gaps_do_not_vote():
    """缺笔剔除不编数：None 不进笔数（不占 P(fp)），全缺就整个让位。"""
    # fp|A 全缺 → 不投票，总分只剩 fp|B 的分
    assert gv.aggregate({"fp|A": [None, None], "fp|B": [0.0]}) == pytest.approx(1.0)
    # 只有缺笔（或压根没笔）→ None：没结算 ≠ 正好猜中
    assert gv.aggregate({"fp|A": [None]}) is None
    assert gv.aggregate({}) is None
    # 缺笔让 fp|A 权重归零：有效笔数只剩 B 那一笔
    assert gv.aggregate({"fp|A": [None, 0.0], "fp|B": [1.0]}) == pytest.approx(
        (1.0 + 0.5) / 2)


def test_pair_form_and_bad_shapes_are_refused():
    """两种形状都收（分桶的 / 逐笔的），形状不对当场说清楚。"""
    assert gv.aggregate([("fp|A", 0.0), ("fp|B", 1.0)]) == pytest.approx(0.75)
    with pytest.raises(ValueError):
        gv.aggregate({"fp|A": 0.7})          # 不是一串 gap
    with pytest.raises(ValueError):
        gv.aggregate([("fp|A",)])             # 不是一对
    with pytest.raises(ValueError):
        gv.aggregate({"fp|A": ["大概"]})      # 话不是数，同一把尺


def test_aggregate_uses_one_scale_for_every_bucket():
    """scale 是腰斩线不是装饰：整条聚合一把尺，传进去情形内折分跟着改。"""
    assert gv.aggregate({"fp|A": [2.0]}, scale=2.0) == pytest.approx(0.5)
    with pytest.raises(ValueError):
        gv.aggregate({"fp|A": [1.0]}, scale=0)


def test_aggregate_wiring_example(tmp_path):
    """接线示例（发生在调用方，不在工具里）：多节各自的 gap → 聚合成一个数 → 回填。"""
    L = Ledger(writer="t", ttl=3600, now=lambda: 300.0)
    keys = []
    for i, (fp, expect, actual) in enumerate(
            [("fp|早", 0.7, 0.5), ("fp|早", 0.9, 0.9), ("fp|晚", 0.4, 0.6)]):
        k = addr(f"聚合示例{i}")
        L.record(k, 1, "判过", fp, ts=100.0 + i)
        L.expect(k, expect, journal=False)
        L.actual(k, actual, journal=False)
        keys.append((fp, k))
    buckets = {}
    for fp, k in keys:
        buckets.setdefault(fp, []).append(L.gap(k))
    v = gv.aggregate(buckets)
    expect_v = (1 / 1.2 + 1.0) / 2            # fp|早：gap 0.2 与 gap 0.0 等权平均
    got = (2 * expect_v + 1 * (1.0 / 1.2)) / 3   # 笔数占比 2:1（fp|晚 gap −0.2）
    assert v == pytest.approx(got)
    rep = addr("聚合代表节")                   # 回不回填、回填哪一圈：调用方定
    L.record(rep, 1, "代表", "fp|早", ts=200.0)
    L.backfill_value(rep, 1, v)
    assert L.by_value(rep) == pytest.approx(v)
    # 工具层仍然不硬绑：上面全是调用方在接线，工具里没有账本
    import ast
    src = open(os.path.join(ROOT, "tools", "gap_value.py"), encoding="utf-8").read()
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    assert "ledger" not in mods


# ── 六、回填时机与时间衰减（round 49：顺序不进聚合，权重进） ──────

HALF = gv.DEFAULT_HALF_LIFE          # 带说明的默认值：一周（604800 秒）


def test_decay_default_half_life_and_missing_age():
    """半衰期到 → 权重对半；没给时间不编时间（原样 1.0）；单位跟调用方走。"""
    assert gv.DEFAULT_HALF_LIFE == 7 * 86400
    assert gv.decay(0) == pytest.approx(1.0)
    assert gv.decay(HALF) == pytest.approx(0.5)
    assert gv.decay(2 * HALF) == pytest.approx(0.25)
    assert gv.decay(None) == pytest.approx(1.0)          # 缺时间不是缺数据
    assert gv.decay(3, half_life=3) == pytest.approx(0.5)  # 按圈数衰减：同单位就行


def test_decay_refuses_bad_age_and_bad_half_life():
    """取舍不是装饰：负年龄、坏半衰期、不是数的 age 当场拒（缺笔也过秤）。"""
    for bad in (-1, "一周", True, [60.0]):
        with pytest.raises(ValueError):
            gv.decay(bad)
    for bad in (0, -3, "一周", None):
        with pytest.raises(ValueError):
            gv.decay(1.0, half_life=bad)
    with pytest.raises(ValueError):                    # 缺笔也不许带坏时间蒙混
        gv.aggregate({"fp|A": [(None, -5)]})
    with pytest.raises(ValueError):
        gv.aggregate({"fp|A": [1.0]}, half_life=0)     # 坏半衰期：没 age 也报


def test_newer_backfill_speaks_louder_and_order_does_not_matter():
    """同一情形两笔：新的那笔算数多（新好 > 老好）；换顺序数不变 —— 顺序不进聚合。"""
    new_good = gv.by_context({"fp|A": [(0.0, 0.0), (1.0, HALF)]})["fp|A"]
    old_good = gv.by_context({"fp|A": [(0.0, HALF), (1.0, 0.0)]})["fp|A"]
    assert new_good == pytest.approx((1.0 * 1 + 0.5 * 0.5) / 1.5)
    assert old_good == pytest.approx((0.5 * 1 + 1.0 * 0.5) / 1.5)
    assert new_good > old_good                        # 新的那笔好 = 分高
    # 求和可交换：谁先谁后一个样，新旧之别全在权重里（不靠排队）
    assert gv.by_context({"fp|A": [(1.0, HALF), (0.0, 0.0)]})["fp|A"] == pytest.approx(new_good)


def test_no_age_keeps_round48_equal_weight_behaviour():
    """不给 age = 不衰减：round 48 的等权/笔数占比一个字不改（回归钉）。"""
    assert gv.by_context({"fp|A": [0.0, 1.0]})["fp|A"] == pytest.approx(0.75)
    assert gv.by_context({"fp|A": [(0.0, None), (1.0, None)]})["fp|A"] == pytest.approx(0.75)
    assert gv.aggregate({"fp|A": [0.0, 0.0, 0.0], "fp|B": [1.0]}) == pytest.approx(0.875)
    assert gv.aggregate({"fp|A": [0.0], "fp|B": [1.0]}) == pytest.approx(0.75)


def test_aggregate_p_is_weight_share_not_raw_count():
    """P(fp) 数的是衰减后的权重：老的那情形让位，不是平分笔数。"""
    v = gv.aggregate({"fp|A": [(1.0, HALF)], "fp|B": [(0.0, 0.0)]})
    # A：score 0.5 × 权重 0.5；B：score 1.0 × 权重 1 → (0.25 + 1.0) / 1.5
    assert v == pytest.approx((0.5 * 0.5 + 1.0) / 1.5)
    assert v != pytest.approx(0.75)                   # 不衰减时是 0.75（笔数各半）


def test_age_shapes_and_bad_shapes_are_refused():
    """两种形状都能带时间（(gap, age) 与三格对）；形状不对当场说清楚。"""
    assert gv.aggregate([("fp|A", 1.0, HALF), ("fp|B", 0.0, 0.0)]) == pytest.approx(
        (0.5 * 0.5 + 1.0 * 1.0) / 1.5)
    with pytest.raises(ValueError):
        gv.aggregate([("fp|A", 1.0, HALF, "多余")])    # 四格不是一对
    with pytest.raises(ValueError):
        gv.aggregate([("fp|A",)])                      # 旧规矩：一格不是一对
    with pytest.raises(ValueError):
        gv.aggregate({"fp|A": [(1.0,)]})               # 带时间只给一格
    with pytest.raises(ValueError):
        gv.aggregate({"fp|A": [(1.0, "一周")]})        # age 不是数
    with pytest.raises(ValueError):
        gv.aggregate({"fp|A": [("大概", 1.0)]})        # 话不是数，同一把尺


def test_missing_gap_with_age_still_does_not_vote():
    """时间救不了缺笔：给了 age 的 None 照旧不投票、不进权重、全缺返回 None。"""
    assert gv.aggregate({"fp|A": [(None, 0.0)], "fp|B": [(0.0, HALF)]}) == pytest.approx(1.0)
    assert gv.aggregate({"fp|A": [(None, 0.0)]}) is None
    assert gv.by_context({"fp|A": [(None, 0.0)]})["fp|A"] is None


def test_decay_is_a_documented_default_still_left_as_a_gap():
    """三件套：默认值在、空格还在、说明写在 docstring 里（含「顺序」这句口径）。"""
    assert "half_life" in gv.decay.__doc__
    assert "half_life" in gv.by_context.__doc__
    assert "half_life" in gv.aggregate.__doc__
    assert "顺序不进聚合" in gv.aggregate.__doc__
    assert "604800" in gv.decay.__doc__               # 默认值不是暗的


def test_wiring_example_with_ages(tmp_path):
    """接线示例（发生在调用方）：带时间的多笔 → 衰减聚合 → 回填读得出；工具仍不碰账。"""
    NOW = 700000.0
    L = Ledger(writer="t", ttl=3600, now=lambda: NOW)
    rows = []
    for i, (fp, expect, actual, ts) in enumerate([
            ("fp|早", 0.7, 0.5, 1000.0),        # 老：距今 699000（超过一个半衰期）
            ("fp|早", 0.9, 0.9, 699900.0),      # 新：距今 100
            ("fp|晚", 0.4, 0.6, 699950.0)]):    # 新：距今 50
        k = addr(f"衰减示例{i}")
        L.record(k, 1, "判过", fp, ts=ts)
        L.expect(k, expect, journal=False)
        L.actual(k, actual, journal=False)
        rows.append((fp, L.gap(k), NOW - ts))          # age 由调用方算：钟在账上
    v = gv.aggregate(rows)
    w = lambda age: 0.5 ** (age / gv.DEFAULT_HALF_LIFE)   # 手算不借工具的秤
    per_early = (gv.score(0.2) * w(699000) + gv.score(0.0) * w(100)) / (w(699000) + w(100))
    expected = (per_early * (w(699000) + w(100)) + gv.score(-0.2) * w(50)) \
        / (w(699000) + w(100) + w(50))
    assert v == pytest.approx(expected)
    # 衰减真起作用了：和等权（不带 age）那个数不一样
    plain = gv.aggregate([(fp, gap) for fp, gap, _ in rows])
    assert v != pytest.approx(plain)
    rep = addr("衰减代表节")                            # 回不回填、回填哪一圈：调用方定
    L.record(rep, 1, "代表", "fp|早", ts=699990.0)
    L.backfill_value(rep, 1, v)
    assert L.by_value(rep) == pytest.approx(v)
    # 工具层仍然不硬绑：上面全是调用方在接线，工具里没有账本
    import ast
    src = open(os.path.join(ROOT, "tools", "gap_value.py"), encoding="utf-8").read()
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    assert "ledger" not in mods

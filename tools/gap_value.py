# -*- coding: utf-8 -*-
"""把「预计 − 实际」的差值折成一个数 —— 可选工具层的示例口径，不硬绑主链。

主链（ledger.py）只给两行格式与差值 `gap()`；怎么折成 `value_score` 一直是
空格（概念稿第 5 条：本件只给格式和公式，**什么算数，使用者定**）。这个模块
把那个空格填成一个**带说明的默认值**，按三件套铁律交差：

    空格   —— `scale`（差多大算腰斩）、要不要回填、回填哪一圈、标签取行为还是
              取嘴说、多笔怎么合成一个数的**权重口径**（情形内等权、情形间按
              笔数占比是本件给的带说明的默认值，要按频次/重要性自己加权就改）、
              **时间衰减**（衰不衰、半衰期多长，都在调用方手里）；
    默认值 —— `scale=1.0`、方向不偏不倚（高估低估一样扣）、缺笔原样传 None、
              聚合按 ctx_fp 分桶后边缘化成一个数、时间衰减半衰期一周
              （`half_life = 604800` 秒，只在笔上给了 `age` 时才起作用）；
    说明   —— 每个取舍都写在 docstring 里，连同「为什么不硬绑」。

公式（最小折算口径）::

    score(gap) = scale / (scale + |gap|)      gap=None 时返回 None

    gap=0（正好猜中）→ 1.0；gap=±scale → 0.5（腰斩）；差得越远越接近 0，
    但永远不等于 0 —— 差得远是「不准」，0 分是「验过不值」，两件事不合并
    （同「缺笔不是 0 分」那条规矩）。

只折数，不碰账：本模块不 import ledger、不写文件、不回填 —— 接不接线、
回填哪一圈（`L.backfill_value(key, rnd, s)`）由调用方定。

例（接线由调用方定，不是本件的默认行为）::

    from tools.gap_value import score
    s = score(L.gap(k))            # None 缺笔原样传下去，不会被当成 0 分
    if s is not None:
        L.backfill_value(k, rnd, s)   # 回不回填、回填哪一圈，使用者定

多笔按 ctx_fp 合成一个数（条件概率表式的边缘化，round 48 示例）::

    from tools.gap_value import aggregate
    buckets = {"fp|店A早": [L.gap(k1), L.gap(k2)],   # 同一情形下若干笔
               "fp|店B晚": [L.gap(k3)]}
    v = aggregate(buckets)        # 一个 value_score（float）；一笔都没有 → None

    两步（先条件、后边缘）::

        情形内  score|fp = mean( score(gap) )   逐笔折分再等权平均
        情形间  value   = Σ P(fp) × (score|fp)  P(fp) = 该情形有效笔数 / 总有效笔数

    只要一个分、不看中间表：`aggregate(...)`。要看各情形的条件分（哪一类猜得准）：
    `by_context(...)` → `{fp: score | None}`。

多笔回填按什么顺序进聚合（round 49）::

    **顺序本身不进聚合** —— 求和可交换，谁先谁后一个样；进聚合的是**权重**。
    新回填说话重、老回填说话轻：时间衰减权重就是「顺序」的默认口径。

        w(age) = 0.5 ** (age / half_life)          情形内  score|fp = Σ w·score / Σ w
        age 没给 → w = 1.0                          情形间  P(fp)    = Σ w(fp) / Σ w 总

    默认半衰期 `half_life = 7 天（604800 秒）`：一周前的那笔回填权重对半。
    `age` 与 `half_life` **同单位**（调用方给什么单位就用什么单位）—— 按圈数衰减
    就两个都用圈数（`half_life=3` = 三圈半衰）。没给 `age` 的笔按满权重进：**不编
    时间，也不替你猜它老**，要严格就每笔都给。给了 `age` 但 `gap=None` 的笔照旧
    不投票（时间救不了缺笔）。

    接线（回填顺序/哪一圈仍是调用方的空格）::

        from tools.gap_value import aggregate
        now = time.time()
        buckets = {"fp|店A早": [(L.gap(k1), now - ts1), (L.gap(k2), now - ts2)]}
        v = aggregate(buckets)     # 新的那笔按满权重、旧的那笔按 0.5**(age/一周)
        L.backfill_value(k, rnd, v)   # 回不回填、回填哪一圈，使用者定

    三件套空格：衰不衰（不给 age 就是等权）、半衰期多长、要不要换成按频次/重要性
    加权 —— 全在调用方手里，本件只给带说明的默认值。
"""


def score(gap, *, scale=1.0):
    """差值折成 0..1 的分：`scale / (scale + |gap|)`。

    参数
    ----
    gap : number | None
        预计 − 实际（`L.gap(key)` 的返回值）。`None` = 缺笔，**原样返回 None** ——
        缺笔不是 0 分，还没结算和「正好猜中」不许混。非数字当场 `ValueError`
        （话不是数，和账本两行同一把尺）。
    scale : float, 关键字参数，默认 1.0
        三件套空格：差多大算「腰斩」。概率型的预计/实际用 1.0 就行；金额、
        次数这类有量纲的，按你自己的尺度调 —— 它只是把差值换算成同一把尺，
        不改变「越准分越高」这个方向。

    口径（说明，不是隐藏选项）
    ----
    - 方向不偏不倚：用 `|gap|`，高估和低估一样扣 —— 要偏袒某一侧，自己在外面改。
    - 单调不归零：score 恒在 (0, 1]，差得再远也只是接近 0 —— 「不准」和
      「验过不值」是两件事，不合并。
    - 只折一个数：单笔怎么变成 0..1 的分是本件；单笔不管新旧。多笔怎么合成一个数见
      `aggregate()`（同样是带说明的默认值），**回填时机/时间衰减**见 `decay()` 与
      `aggregate(..., half_life=)`：回不回填、回填哪一圈仍留给使用者。
    """
    if gap is None:
        return None                       # 缺笔不是 0 分：原样传，不替你编一个数
    if isinstance(gap, bool) or not isinstance(gap, (int, float)):
        raise ValueError(
            f"折算只收数字（差值才算得出来），收到 {gap!r}。"
            "话不是数：要记一句话，请落成一节判决（record）。")
    if isinstance(scale, bool) or not isinstance(scale, (int, float)) or scale <= 0:
        raise ValueError(f"scale 要是正数（差多大算腰斩），收到 {scale!r}。")
    return scale / (scale + abs(gap))


def _half_life(half_life):
    """半衰期必须是正数（和 age 同单位）—— 取舍不是装饰，坏的当场拒。"""
    if isinstance(half_life, bool) or not isinstance(half_life, (int, float)) or half_life <= 0:
        raise ValueError(
            f"half_life 要是正数（半衰期，和 age 同一个单位），收到 {half_life!r}。")
    return float(half_life)


DEFAULT_HALF_LIFE = 7 * 86400       # 秒：一周（带说明的默认值，见 decay()）


def decay(age, *, half_life=DEFAULT_HALF_LIFE):
    """时间衰减权重：`0.5 ** (age / half_life)` —— 过一个半衰期，权重对半。

    参数
    ----
    age : number | None
        这笔回填距今多久（**和 `half_life` 同单位**，默认按秒）。`None` = 没给
        时间 → **原样返回 1.0（满权重）**：不编时间，也不替你猜它老。负数当场
        `ValueError`（回填还没发生），非数字（含 `bool`）当场 `ValueError`。
    half_life : float, 关键字参数，默认 604800（7 天）
        三件套空格：衰多快。半衰期到，权重正好 0.5；按圈数衰减就传圈数
        （`half_life=3` = 三圈半衰）—— 只要与 `age` 同单位就行。

    口径（说明，不是隐藏选项）
    ----
    - 顺序不进聚合、权重进：求和可交换，谁先谁后一个样；「新回填说话重」是靠
      这个权重落的，不是靠排序（要排序自己在外面排）。
    - 缺时间按满权重（1.0）：宁可不衰减也不编一个年龄 —— 同「缺笔不是 0 分」。
      要严格，每笔都给 `age`。
    - 只给权重，不碰账：回不回填、回填哪一圈，仍然在调用方手里。
    """
    half_life = _half_life(half_life)
    if age is None:
        return 1.0
    if isinstance(age, bool) or not isinstance(age, (int, float)):
        raise ValueError(
            f"age 要是数字（回填距今多久，和 half_life 同单位），收到 {age!r}。")
    if age < 0:
        raise ValueError(f"age 不能是负数（回填还没发生），收到 {age!r}。")
    return 0.5 ** (age / half_life)


def _split(g):
    """一笔 → `(gap, age)`：裸的 gap = 没给时间；`(gap, age)` 一对 = 带时间。"""
    if isinstance(g, (tuple, list)):
        if len(g) != 2:
            raise ValueError(
                f"带时间的一笔要写成 (gap, age) 两格，收到 {g!r}。")
        return g[0], g[1]
    return g, None


def _pairs(buckets):
    """把两种输入形状拍平成 `[(fp, gap, age), ...]`，并当场把形状说清楚。

    形状一 `{fp: [笔, ...]}` —— 按情形分好桶的（最常用）；每笔是 gap，
    或 `(gap, age)`（带时间，用来做时间衰减）。
    形状二 `[(fp, gap), …]` 或 `[(fp, gap, age), …]` —— 逐笔带指纹的流水
    （`for k in keys: (fp, L.gap(k))`，要衰减就多带一格 age）。
    """
    if hasattr(buckets, "items"):
        out = []
        for fp, gs in buckets.items():
            if isinstance(gs, (str, bytes)) or not hasattr(gs, "__iter__"):
                raise ValueError(
                    f"情形 {fp!r} 要给一串 gap，收到 {gs!r}（缺一笔也得占个 None 位）。")
            for g in gs:
                gap, age = _split(g)
                out.append((fp, gap, age))
        return out
    out = []
    for item in buckets:
        if not isinstance(item, (tuple, list)) or len(item) not in (2, 3):
            raise ValueError(
                f"逐笔形状要 (fp, gap) 或 (fp, gap, age) 一对一对给，收到 {item!r}。")
        if len(item) == 3:
            out.append((item[0], item[1], item[2]))
        else:
            gap, age = _split(item[1])
            out.append((item[0], gap, age))
    return out


def by_context(buckets, *, scale=1.0, half_life=DEFAULT_HALF_LIFE):
    """条件层：每个情形（`ctx_fp`）一个分 —— 该情形下多笔先各自折分、再**加权**平均。

    返回 `{fp: score | None}`。某一情形里**全是缺笔** → 该情形 `None`（没结算就是
    没结算，不替你编一个数）。缺笔不参与平均（分母只数折得出来的）。

    为什么先折后平均、不先平均 gap：先在差值层求和，高估 `+1` 和低估 `-1`
    会互相抵消成 `0`（看着像猜中）；先各自折成分，方向不偏不倚那条才守得住 ——
    `|gap|` 在**逐笔**上取，平均在**分数**上取。

    权重（round 49 默认值）：`w = decay(age, half_life=half_life)` —— 给了 `age`
    的笔按新旧说话（默认一周半衰），没给的按 1.0（等权，也就是 round 48 那版的
    行为）。**三件套空格**：不给 `age` = 不衰减、`half_life` 自己定、要按重要性
    加权就自己在外面加权再喂进来。

    仍然不碰账：不 import ledger、不回填。
    """
    _half_life(half_life)
    acc = {}
    for fp, gap, age in _pairs(buckets):
        w = decay(age, half_life=half_life)      # 先过秤：坏 age 当场报（缺笔也过）
        acc.setdefault(fp, []).append((score(gap, scale=scale), w))
    out = {}
    for fp, items in acc.items():
        live = [(s, w) for s, w in items if s is not None]
        out[fp] = sum(s * w for s, w in live) / sum(w for _, w in live) if live else None
    return out


def aggregate(buckets, *, scale=1.0, half_life=DEFAULT_HALF_LIFE):
    """边缘层：多笔 gap 按 `ctx_fp` 合成**一个** value_score（条件概率表式）。

    两步走（这就是条件概率表的用法：先按情形给条件分，再按情形的实测占比合）::

        情形内  score|fp = Σ w·score / Σ w      ← by_context()（w=1 时就是 mean）
        情形间  value   = Σ P(fp) × (score|fp)   P(fp) = 该情形权重和 / 总权重

    参数
    ----
    buckets : mapping {fp: [笔, …]} | iterable [(fp, gap[, age]), …]
        每笔 gap 是 `L.gap(key)` 的返回值（预计 − 实际），`None` = 缺笔；
        要时间衰减就写成 `(gap, age)`（`age` 与 `half_life` 同单位）。
    scale : float, 关键字参数，默认 1.0
        同 `score()` 的腰斩线：情形内折分时用它，整条链一把尺。
    half_life : float, 关键字参数，默认 604800（7 天）
        时间衰减的半衰期（见 `decay()`）。**没给 `age` 的笔权重全是 1.0 —— 整条
        链退化成 round 48 的「笔数占比」**，行为一个字不改。

    返回
    ----
    float | None —— 落在 (0, 1] 的一个数；**一笔有效数据都没有就返回 `None`**
    （全缺笔、或整个输入是空的）：没结算和「正好猜中」不许混，同「缺笔不是 0 分」。

    口径（说明，不是隐藏选项）
    ----
    - 权重 = 时间衰减后的笔数占比：这正是从条件概率表边缘化时的 `P(fp)` ——
      出得多、又新的情形说话重。**这是空格**：不衰减（不给 age）、半衰期多长、
      按重放频率/重要性加权、按情形分层各占一份，都在调用方手里（本件只给带
      说明的默认权重）。
    - 顺序不进聚合：谁先谁后一个样（求和可交换），新旧之别全在 `decay()` 那个
      权重里 —— 要「按时间从新到旧依次进」这种排序口径，自己在外面排。
    - 缺笔剔除不编数：`None` 不进分母也不进分子；某情形全缺 → 该情形整个让位。
    - 不硬绑主链：不 import 账本、不写文件、不回填。合出来的这一个数要不要
      `backfill_value`、回填哪一圈，仍然是调用方的事（接线示例见 docstring 顶部
      与测试）。
    """
    _half_life(half_life)
    pairs = _pairs(buckets)
    weights = {}
    for fp, gap, age in pairs:
        w = decay(age, half_life=half_life)      # 坏 age 不许蒙混：先过秤
        if gap is None:
            continue                       # 缺笔不占权重：P(fp) 只数真折出来的
        weights[fp] = weights.get(fp, 0.0) + w
    total = sum(weights.values())
    if not total:
        return None                         # 一笔有效数据都没有 = 还没结算，不编数
    per = by_context(pairs, scale=scale, half_life=half_life)
    return sum(per[fp] * w / total for fp, w in weights.items())

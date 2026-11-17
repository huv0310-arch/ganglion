"""该不该记的判据（admission）—— 2026-10-31 probe 定的那套，2026-11-06 接到记账口。

账本（ledger.py）只提供闸：``Ledger(admit=...)``，闸里判什么由这里说了算。
判的和记的必须是两条链（双螺旋）：判据写错了，账本不该跟着错 —— 所以判据
单独一个文件，ledger.py 里一个业务词都不许有。

判据：期望收益 = 重放概率 × 重算成本 − 写入成本
  收益 > 0  → STORE  落账（下次同内容直接借，省下重算）
  收益 ≤ 0  → DROP   不落（这一节留着只是把账本养爆）

接法（一行）::

    import admission
    L = Ledger(writer="w", ttl=3600, admit=admission.gate("tree_search"))

不给 ``admit`` = 不拦、全记 —— 账本不知道你的重放概率，那是调用方的事。
"""

PROFILES = {
    # 这些数是实测出来的，不是拍的：
    #   重放概率 = 同一内容被再次取用的频率（短命进程里 90% 的 key 只用一次）
    #   重算成本 = 再算一遍要花多久（秒）
    "dp_overlapping": {
        "replay": 0.62,
        "cost": 2041.0,
        "note": "DP 隔行重叠：同一子问题实测被取用 6,284 次",
    },
    "tree_search": {
        "replay": 0.028,
        "cost": 2041.0,
        "note": "树搜大量撞树，实测 43,062 条死路账（短命进程里 90% 的 key 只用一次）",
    },
    "judgment_repeat": {
        "replay": 0.048,
        "cost": 1.0,
        "note": "反射类判定全路径（实测 10-31）",
    },
    "echo_storm": {
        "replay": 0.17,
        "cost": 0.1,
        "note": "回声抑制路径（实测 10-31）",
    },
}


def decide(replay_prob, recompute_cost, write_cost=1.0):
    """算这一节该不该落账。

    replay_prob   重放概率（0~1）：这内容以后还会不会再被取用
    recompute_cost 重算成本（秒）：不落账的话，下次要花多久重算
    write_cost    写入成本（秒，默认 1）

    返回 (action, gain)，action ∈ {"STORE", "DROP"}，gain = 期望收益（秒）。
    """
    gain = replay_prob * recompute_cost - write_cost
    if gain > 0:
        return "STORE", gain
    return "DROP", gain


def decide_for(kind, write_cost=1.0):
    """给一个已知 workload kind，算出判据。

    kind ∈ PROFILES 的 key。
    """
    p = PROFILES[kind]
    return decide(p["replay"], p["cost"], write_cost)


def gate(kind=None, replay=None, cost=None, write_cost=1.0):
    """造一个能直接喂给 ``Ledger(admit=...)`` 的闸：``f(node) -> bool``。

    kind 给 PROFILES 里的现成档；或者自己给 replay 和 cost。
    两样都不给就报错 —— 零默认值：账本不知道你的重放概率，不替你猜。

    闸是**按档**的，不按节：同一个 workload 的每一节都过同一个判据。
    要按节细判（比如只拦某一类 key），自己写 ``lambda node: ...`` 传进去，
    闸只认返回值真假，不认是谁写的。
    """
    if kind is not None:
        p = PROFILES[kind]
        replay, cost = p["replay"], p["cost"]
    elif replay is None or cost is None:
        raise ValueError(
            "gate() 要么给 kind（PROFILES 里的档），要么同时给 replay 和 cost。"
            "不给就不许猜 —— 猜出来的重放概率是假数。"
        )
    action, gain = decide(replay, cost, write_cost)
    keep = action == "STORE"

    def _f(node):
        return keep

    _f.action = action
    _f.gain = gain
    return _f


def _selftest():
    assert decide_for("tree_search")[0] == "STORE"
    assert decide_for("tree_search")[1] > 0
    assert decide_for("judgment_repeat")[0] == "DROP"
    print("admission OK")


if __name__ == "__main__":
    _selftest()

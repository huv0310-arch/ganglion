# -*- coding: utf-8 -*-
"""外部使用者（协作者 B）第一轮实测抓到的三处，各钉一条测试。"""
from ganglion import _make_key
from ledger import Ledger, addr


def test_unresolved_means_outstanding_not_lifetime():
    """协作者 B报的：悬位明明是空的，stat['unresolved'] 还是 1。

    "现在悬着几笔"必须现数，存一个计数器迟早走样。
    """
    L = Ledger(writer="t")
    k = _make_key("端口通不通")
    L.open_pending(k, why="evidence", need="等探针")
    assert L.to_dict()["stat"]["unresolved"] == 1
    L.resolve(k, 1, "通了", journal=False)
    assert L.to_dict()["stat"]["unresolved"] == 0        # 现在一笔不悬
    assert L.to_dict()["stat"]["opened"] == 1            # 累计开过一笔


def test_window_is_frozen_into_the_round_at_write_time():
    """协作者 B报的：Ledger(writer="t", ttl=60) 记的账，节上 ttl=None。

    账本级 ttl 一改，历史节的含意跟着变 —— 等于改了对过去的解释。
    """
    L = Ledger(writer="t", ttl=60)
    n = L.record(_make_key("端口通不通"), 1, "通", "fp|a")
    assert n["ttl"] == 60
    L.ttl = 5
    assert L.nodes[n["key"]][0]["ttl"] == 60             # 写进去的窗不许被后来的改掉


def test_addr_is_public_and_matches_internal():
    """协作者 B报的：外部想生成同款地址只能抄私货 _make_key。"""
    assert addr("某台车合规") == _make_key("某台车合规")
    assert addr("同一件事") == addr("同一件事")
    assert addr("甲") != addr("乙")

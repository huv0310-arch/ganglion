# -*- coding: utf-8 -*-
"""不变量的公共尺（round 53）—— 压测与随机不变量测试共用同一套判据。

三条（每一条都不调被测的排序/计数来给自己作证）：

  ① 链不断      每个地址的圈号必须是 1..n 连号、节上的 key 必须就是这个地址。
                 返回第一个断点；`None` = 没断。
  ② 行数守恒    落到链上的节数（append/revisit 计数）= 各地址链长之和
                 = size()（活着的）+ tombstones()（墓碑）。三把刀（墓碑/褪色/压实）
                 都只改内容不抽行 —— 抽走一行，账上就少一段过去。
  ③ 时间线对得上 recent(n) 的结果 = 自己按「ts 降序 → 圈号降序 → 地址升序」
                 独立排一遍的前 n（墓碑不算、没有钟的跳过、n<=0 给 []）。

tools/scale_stress.py 里有一份同口径的拷贝（那个工具要能单独拿出去跑，不 import 测试目录）；
改口径请两处一起改。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger import Ledger  # noqa: E402


def chain_break_at(L):
    """① 链校验：圈号 1..n 连号且节上的 key = 地址。返回第一个断点，没断给 None。"""
    for key, chain in L.nodes.items():
        for i, node in enumerate(chain or ()):
            if node.get("round") != i + 1:
                return f"{key} 第 {i + 1} 节圈号是 {node.get('round')!r}（该是 {i + 1}）"
            if node.get("key") != key:
                return f"{key} 第 {i + 1} 节的 key 是 {node.get('key')!r}（节和地址对不上）"
    return None


def total_rows(L):
    """② 链上一共有几节（含墓碑 —— 墓碑占着圈号，也是账上的一段过去）。"""
    return sum(len(chain or ()) for chain in L.nodes.values())


def expected_recent(L, n):
    """③ 独立算一遍 recent 的口径：不调 L.recent，免得拿被测代码给被测代码作证。"""
    if n is None or n <= 0:
        return []
    out = []
    for chain in L.nodes.values():
        for node in chain or ():
            if L.is_tombstone(node):
                continue
            if not isinstance(node.get("ts"), (int, float)):
                continue
            out.append(node)
    out.sort(key=lambda m: (-m["ts"], -(m.get("round") or 0), m.get("key") or ""))
    return out[:n]


def recent_matches(L, n):
    """recent(n) 与独立口径逐节对上（按 nid 认节，不比内容）。"""
    return [x.get("nid") for x in L.recent(n)] == [x.get("nid") for x in expected_recent(L, n)]


def check_invariants(L, expected_rows=None, where=""):
    """三条一起查，返回错误清单（空表 = 全过）。`where` 只是报错时的现场说明。"""
    errs = []
    broke = chain_break_at(L)
    if broke:
        errs.append(f"① 链断了：{broke}")
    total = total_rows(L)
    if expected_rows is not None and total != expected_rows:
        errs.append(f"② 行数不守恒：链上 {total} 节，落账时 {expected_rows} 节")
    if L.size() + L.tombstones() != total:
        errs.append(f"② 活着的 {L.size()} + 墓碑 {L.tombstones()} != 链上 {total} 节")
    for n in (0, 1, 5, 10_000):
        if not recent_matches(L, n):
            errs.append(f"③ recent({n}) 与独立口径不一致")
    if errs and where:
        return [f"[{where}] {e}" for e in errs]
    return errs

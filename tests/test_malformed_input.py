# -*- coding: utf-8 -*-
"""损坏与恶意输入鲁棒性（round 60）—— 八类坏文件喂 `load` / 读入口，每类三问。

round 56/57/58/59 把**正常输入、真并行、崩在半路、盘满、长跑稳态**都验完了；
这一份补最后一块空白：**账本文件是外部可触的** —— 谁塞个坏文件进来，
都不能把它打死、挂死、撑爆，也不能让它把坏数据吞了假装读全了。

corpus 八类（每类三问）：

  ① 截断的 JSON 行   半行、空行、纯空格行混在一起，末尾还挂一条没换行的残尾
  ② 二进制垃圾       固定种子 4096 字节随机字节（含 \\x00 / \\xff），流水与压实底都塞
  ③ 超长单行         ≥10 MB 一行（合法的与截断的各一条）
  ④ 深嵌套           10⁵ 层 `[...]`（流水一条 + 快照一份）—— 防递归炸
  ⑤ 非法 UTF-8 字节  \\xff\\xfe 藏在流水的行里、也藏在快照里
  ⑥ 字段类型炸弹     round 是对象/数组、判决是话、整行不是对象、t 不是类型、容器类型不对
  ⑦ 重复 nid 的两节   同一节写两遍（内容一样的、内容不一样的各一对）
  ⑧ 流水与底对不上   底比流水新：`_journal_upto` 越界 / 底有流水里没有的节

三问（任务书原话）：

  ① 打死没有     不许未捕获的 RecursionError / MemoryError 级崩溃，不许进程挂死
                 （超时即红）、不许撑爆（RSS 超上限即红）
  ② 报得诚实吗   该抛的抛**带原因**（点名是哪个文件、哪一格）、该进 `bad_lines` 的进、
                 绝不吞掉假装正常
  ③ 能继续用吗   坏文件处理之后，同目录正常 append / load 还能干活；**或者**明确拒绝
                 并留全原文件（一个字节都不许动），挪开之后照常干活 —— 不许静默丢用户数据

**每一类都在子进程里跑**：父进程掐表（`BUDGET_S = 10` 秒，挂了 / 超了当场 kill 判红），
子进程自己采样自己的 RSS 峰值（`MEM_CAP_MB = 400`，撑爆即红 —— 父进程跨进程读出来的
RSS 在这台机器上不可信：实测父看子 3 MB、子自报 15.9 MB，差 5 倍，拿它当上限等于没有）。
固定种子 `SEED = 20261107`；全程 pytest 临时目录，真实账一个字节不碰。单类跑法（排障用）：
`python tests/test_malformed_input.py --run <类名> <目录>`。
"""
import json
import os
import random
import subprocess
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ledger import Ledger, addr                            # noqa: E402
from _invariants import chain_break_at, recent_matches     # noqa: E402

SEED = 20261107               # 固定：换台机器同一条垃圾序列
BUDGET_S = 10.0               # 任务书硬线：单类 ≤ 10 秒
MEM_CAP_MB = 400              # 撑爆即红（子进程 RSS 上限）
BASE_ROWS = 3                 # 每类先落的正经账：坏文件打完，一节不许少
DEEP = 10 ** 5                # 深嵌套层数（任务书：10⁵ 层）
GIANT = 10 * 1024 * 1024      # 超长单行（任务书：≥10 MB 一行）
K = addr("损坏输入现场")
K2 = addr("损坏输入·续用现场")


# ══════════════ 尺子 ══════════════

def _call(fn):
    """跑一件事，把返回值与异常都交出来（三问要逐条攒，不能撞一次就断）。"""
    try:
        return fn(), None
    except BaseException as e:                     # noqa: BLE001 —— 要的就是把异常交出来判
        return None, e


def _err(e):
    return f"{type(e).__name__}: {e}"


def _rows(L):
    return sum(len(c or ()) for c in L.nodes.values())


def _bad(L):
    return int(L.stat.get("bad_lines") or 0)


def _res(q1, q2, q3, note=""):
    return {"q1": list(q1), "q2": list(q2), "q3": list(q3), "note": note}


def _base(d, tag, n=BASE_ROWS):
    """一本来路清白的账（n 节 + 快照）：所有坏文件都打在它上面 / 旁边。"""
    sub = os.path.join(d, tag)
    os.makedirs(sub, exist_ok=True)
    snap = os.path.join(sub, "book.json")
    jp = snap + ".jsonl"
    L = Ledger(writer="probe", ttl=None)
    for i in range(n):
        line = L.append(jp, K, 1 if i % 2 else 0, reason=f"第{i + 1}节",
                        ctx_fp=f"fp{i}", writer="probe")
        assert line is not None, "前置落账被准入闸拦下了（口径不该变）"
    L.save(snap)
    return snap, jp


def _load_ok(snap, q1, what):
    """① 这一类的 load 必须开得出来 —— 开不出来就是被打死了。"""
    L, e = _call(lambda: Ledger.load(snap, writer="probe", empty_ok=True))
    if e is not None:
        q1.append(f"① {what} 打死了：{_err(e)}")
    return L, e


def _book_ok(L, want_rows, q, where):
    """② 通用尺：行数守恒、链不断、recent 与独立口径对得上。"""
    if L is None:
        return
    if _rows(L) != want_rows:
        q.append(f"② {where}：链上 {_rows(L)} 节（该 {want_rows}）")
    b = chain_break_at(L)
    if b:
        q.append(f"② {where}：链断了：{b}")
    if not recent_matches(L, 3):
        q.append(f"② {where}：recent 与独立口径对不上")


def _last_line_ok(jp):
    """流水最后一行 parse 得动 —— 新写那行没粘在残尾上（粘了就是坏一行丢一行）。"""
    try:
        with open(jp, "rb") as f:
            raw = f.read()
    except OSError:
        return False
    if not raw.endswith(b"\n"):
        return False
    last = raw.rstrip(b"\n").split(b"\n")[-1]
    if not last.strip():
        return True
    try:
        json.loads(last.decode("utf-8"))
    except Exception:                              # noqa: BLE001
        return False
    return True


def _continue(snap, jp, want_rows, q3, what):
    """③ 同目录还能干活：重开 → append → 再重读，行数守恒、链不断、新行落稳。"""
    L, e = _call(lambda: Ledger.load(snap, writer="probe", empty_ok=True))
    if e is not None:
        q3.append(f"③ {what}：坏文件之后 load 不动了：{_err(e)}")
        return
    line, e2 = _call(lambda: L.append(jp, K2, 1, "坏文件之后那一节",
                                      "fp|after", writer="probe"))
    if e2 is not None:
        q3.append(f"③ {what}：append 不动了：{_err(e2)}")
        return
    if line is None:
        q3.append(f"③ {what}：append 被准入闸拦下（口径不该变）")
        return
    L2, e3 = _call(lambda: Ledger.load(snap, writer="probe", empty_ok=True))
    if e3 is not None:
        q3.append(f"③ {what}：append 之后重读不动：{_err(e3)}")
        return
    if _rows(L2) != want_rows + 1:
        q3.append(f"③ {what}：行数不守恒：重读 {_rows(L2)} 节（该 {want_rows + 1}）")
    b = chain_break_at(L2)
    if b:
        q3.append(f"③ {what}：链断了：{b}")
    if not _last_line_ok(jp):
        q3.append(f"③ {what}：新写那一行没落稳（流水最后一行 parse 不动）")
    if not recent_matches(L2, 3):
        q3.append(f"③ {what}：recent 与独立口径对不上")


def _refuse(snap, q1, q2, q3, what):
    """坏快照的正确姿势（三问一起问）：

      ① 不许是进程级灾难（RecursionError / MemoryError 不许冒到 load 外面）
      ② **明确拒绝**并点名是哪个文件，原文件一个字节都不许动
      ③ 挪开坏本之后，同目录照常 append / load
    """
    with open(snap, "rb") as f:
        before = f.read()
    L, e = _call(lambda: Ledger.load(snap, writer="probe", empty_ok=True))
    if e is None:
        q2.append(f"② {what}：坏快照被静默收下了（load 一声不吭，链上 {_rows(L)} 节）")
        return False
    if isinstance(e, (RecursionError, MemoryError)):
        q1.append(f"① {what}：{type(e).__name__} 冒到 load 外面 = 进程级灾难：{e}")
        return False
    msg = str(e).strip()
    if not msg:
        q2.append(f"② {what}：拒绝了却一句原因都没有（{type(e).__name__}）")
    if os.path.basename(snap) not in msg:
        q2.append(f"② {what}：拒绝了但没点名是哪个文件：{type(e).__name__}: {msg[:160]}")
    with open(snap, "rb") as f:
        if f.read() != before:
            q2.append(f"② {what}：拒绝却动了原文件一个字节（不许静默丢用户数据）")
    # ③ 挪开坏本，同目录照常干活
    os.replace(snap, snap + ".corrupt")
    L2, e2 = _call(lambda: Ledger.load(snap, writer="probe", empty_ok=True))
    if e2 is not None:
        q3.append(f"③ {what}：坏本挪开之后还是 load 不动：{_err(e2)}")
        return True
    line, e3 = _call(lambda: L2.append(snap + ".jsonl", K2, 1, "挪开坏本之后那一节",
                                       "fp|refuse", writer="probe"))
    if e3 is not None or line is None:
        q3.append(f"③ {what}：坏本挪开之后 append 不动："
                  f"{_err(e3) if e3 else '被准入闸拦下'}")
        return True
    L3, e4 = _call(lambda: Ledger.load(snap, writer="probe", empty_ok=True))
    if e4 is not None:
        q3.append(f"③ {what}：append 之后重读不动：{_err(e4)}")
    else:
        if _rows(L3) != 1:
            q3.append(f"③ {what}：挪开坏本后新账 {_rows(L3)} 节（该 1）")
        b = chain_break_at(L3)
        if b:
            q3.append(f"③ {what}：链断了：{b}")
    return True


# ══════════════ corpus 八类 ══════════════

def run_truncated(d):
    """① 截断的 JSON 行（半行 / 空行混杂）。"""
    q1, q2, q3 = [], [], []
    snap, jp = _base(d, "t1")
    with open(jp, "a", encoding="utf-8", newline="\n") as f:
        f.write('{"t": "node", "key": "k", "node": {"nid": "half", "verdict": 1}\n')  # 半行
        f.write("\n\n   \n")                                        # 空行、纯空格行
        f.write('{"t": "hit", "key": "k", "round": 1)\n')           # 括号坏了
        f.write('{"t": "pending", "key": "k", "p": {"why": ')        # 末尾残尾（没换行）
    L, e = _load_ok(snap, q1, "截断行的 load")
    if e is None:
        _book_ok(L, BASE_ROWS, q2, "截断行")
        if _bad(L) < 2:
            q2.append(f"② 两条带换行的坏行被吞了：bad_lines={_bad(L)}（该 ≥2）")
    _continue(snap, jp, BASE_ROWS, q3, "截断行")
    return _res(q1, q2, q3, "半行 / 空行混杂 + 末尾残尾")


def run_binary(d):
    """② 二进制垃圾（含 \\x00、随机字节）—— 流水和压实底都塞。"""
    q1, q2, q3 = [], [], []
    snap, jp = _base(d, "t2")
    rng = random.Random(SEED)
    with open(jp, "ab") as f:
        f.write(bytes(rng.randrange(256) for _ in range(4096)) + b"\n")
        f.write(b"\x00\x01\x02\xff\xfe\x80\x81\n")
    # 压实底也是坏的：load 会去读 `*.compact`，那份也得吃得动
    with open(jp + ".1234.5.compact", "wb") as f:
        f.write(bytes(rng.randrange(256) for _ in range(2048)) + b"\n")
    L, e = _load_ok(snap, q1, "二进制垃圾的 load")
    if e is None:
        _book_ok(L, BASE_ROWS, q2, "二进制垃圾")
        if _bad(L) < 3:
            q2.append(f"② 垃圾字节被吞了：bad_lines={_bad(L)}（该 ≥3：流水两坨 + 坏底一坨）")
    _continue(snap, jp, BASE_ROWS, q3, "二进制垃圾")
    return _res(q1, q2, q3, f"固定种子 {SEED} 的 4096+8 字节 + 2048 字节坏底")


def run_giant(d):
    """③ 超长单行（≥10 MB 一行）—— 合法的与截断的各一条。"""
    q1, q2, q3 = [], [], []
    snap, jp = _base(d, "t3")
    with open(jp, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"t": "hit", "key": K, "round": 1,
                            "pad": "A" * GIANT}) + "\n")             # 合法的 10 MB 行
        f.write('{"t": "hit", "key": "' + "B" * GIANT)               # 截断的 10 MB 行（无换行）
    L, e = _load_ok(snap, q1, "10MB 单行的 load")
    if e is None:
        _book_ok(L, BASE_ROWS, q2, "10MB 单行")
    _continue(snap, jp, BASE_ROWS, q3, "10MB 单行")
    return _res(q1, q2, q3, f"10 MB 合法行 + 10 MB 截断行（流水 {os.path.getsize(jp)} 字节）")


def run_deep(d):
    """④ 深嵌套 10⁵ 层：流水一条 + 快照一份（防递归炸）。"""
    q1, q2, q3 = [], [], []
    # (a) 流水里一条深的，后面跟一条好行 —— 一条坏行不许把整本关在门外
    snap, jp = _base(d, "t4j")
    with open(jp, "a", encoding="utf-8", newline="\n") as f:
        f.write("[" * DEEP + "]" * DEEP + "\n")
        f.write(json.dumps({"t": "node", "key": K,
                            "node": {"key": K, "nid": "deep_ok", "verdict": 1,
                                     "ts": 9.0, "reason": "深嵌套后面那条好行",
                                     "ctx_fp": "fp|deep"}}) + "\n")
    L, e = _load_ok(snap, q1, "深嵌套行的 load")
    if e is None:
        _book_ok(L, BASE_ROWS + 1, q2, "深嵌套行")
        if _bad(L) < 1:
            q2.append(f"② 深嵌套行没进 bad_lines：{_bad(L)}（该 ≥1）")
    _continue(snap, jp, BASE_ROWS + 1, q3, "深嵌套行")
    # (b) 快照整份深嵌套
    snap2 = os.path.join(d, "t4s", "book.json")
    os.makedirs(os.path.dirname(snap2), exist_ok=True)
    with open(snap2, "w", encoding="utf-8", newline="\n") as f:
        f.write('{"nodes": ' + "[" * DEEP + "]" * DEEP + "}")
    _refuse(snap2, q1, q2, q3, "深嵌套快照")
    return _res(q1, q2, q3, f"流水 10⁵ 层 + 快照 10⁵ 层")


def run_invalid_utf8(d):
    """⑤ 非法 UTF-8 字节：藏在流水的行里、也藏在快照里。"""
    q1, q2, q3 = [], [], []
    snap, jp = _base(d, "t5j")
    with open(jp, "ab") as f:
        f.write(b'{"t": "hit", "key": "\xff\xfe\xfa", "round": 1}\n')
        f.write(b'{"t": "node", "key": "BADKEY", "node": {"key": "BADKEY",'
                b' "nid": "\xff\xfeX", "verdict": 1,'
                b' "reason": "\xe5\x9d\x8f\xe5\xad\x97\xe8\x8a\x82",'
                b' "ts": 7.0}}\n')
    L, e = _load_ok(snap, q1, "乱码行的 load")
    if e is None:
        _book_ok(L, BASE_ROWS, q2, "乱码行")
        if _bad(L) < 2:
            q2.append(f"② 两条乱码行被吞了：bad_lines={_bad(L)}（该 ≥2）")
        if "BADKEY" in L.nodes:
            q2.append("② 非法字节被「修好」成一节真账进了链（nid 变成替换字符）——"
                      " 损坏字节不许伪装成有效记录")
    _continue(snap, jp, BASE_ROWS, q3, "乱码行")
    # 另一个读入口 `replay()`：同一份数据，两个口径必须一致
    if L is not None:
        _, e_r = _call(lambda: L.replay(jp))
        if e_r is not None:
            q1.append(f"① 另一个读入口 replay 当场抛（load 吃得动、它吃不动）：{_err(e_r)}")
    # (b) 快照里有非法字节
    snap2 = os.path.join(d, "t5s", "book.json")
    os.makedirs(os.path.dirname(snap2), exist_ok=True)
    with open(snap2, "wb") as f:
        f.write(b'{"nodes": {}, "stat": {"bad_lines": \xff\xfe}}')
    _refuse(snap2, q1, q2, q3, "乱码快照")
    return _res(q1, q2, q3, "流水两行乱码 + 快照乱码")


def run_type_bomb(d):
    """⑥ 字段类型炸弹：round 是对象 / 数组、判决是话、整行不是对象、容器类型不对。"""
    q1, q2, q3 = [], [], []
    snap, jp = _base(d, "t6j")
    with open(jp, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"t": "node", "key": K,
                            "node": {"nid": "tb1", "verdict": 1, "round": {"a": 1}}}) + "\n")
        f.write(json.dumps({"t": "hit", "key": K, "round": [1, 2],
                            "ctx_fp": ["x"] * 100000}) + "\n")       # round 是数组 + 巨型 ctx_fp
        f.write(json.dumps({"t": "node", "key": K,
                            "node": {"nid": "tb2", "verdict": "对"}}) + "\n")
        f.write("123\n")                                             # 整行不是对象
        f.write(json.dumps({"t": 123, "key": K}) + "\n")             # t 不是类型
    L, e = _load_ok(snap, q1, "类型炸弹的 load")
    if e is None:
        _book_ok(L, BASE_ROWS, q2, "类型炸弹")
        if _bad(L) < 5:
            q2.append(f"② 五条类型炸弹只认了 {_bad(L)} 条（该 ≥5）——"
                      " 认不出来的行不许当成「已重演」")
        if any(n.get("nid") in ("tb1", "tb2")
               for c in L.nodes.values() for n in c):
            q2.append("② 类型炸弹那一节进了链")
    _continue(snap, jp, BASE_ROWS, q3, "类型炸弹")
    # (b) 快照上的类型炸弹：容器不对 / 字段不对
    for tag, body in (("容器", {"nodes": [], "stat": [], "open": []}),
                      ("字段", {"nodes": {"k": [{"nid": "x", "round": "1",
                                                 "verdict": "对", "key": "k"}]},
                                "stat": {}})):
        p = os.path.join(d, f"t6{tag}", "book.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(body, f, ensure_ascii=False)
        _refuse(p, q1, q2, q3, f"类型炸弹快照（{tag}）")
    return _res(q1, q2, q3, "流水五条炸弹 + 快照容器/字段各一份")


def run_dup_nid(d):
    """⑦ 重复 nid 的两节：内容一样的写两遍、内容不一样的同一个号写两遍。"""
    q1, q2, q3 = [], [], []
    snap, jp = _base(d, "t7")
    same = {"t": "node", "key": K, "node": {"key": K, "nid": "DUPA", "verdict": 1,
                                            "reason": "原样写两遍的那一节", "ts": 2.0}}
    twin_a = {"t": "node", "key": K, "node": {"key": K, "nid": "DUPB", "verdict": 1,
                                              "reason": "一号两内容·甲", "ts": 3.0, "round": 2}}
    twin_b = {"t": "node", "key": K, "node": {"key": K, "nid": "DUPB", "verdict": 0,
                                              "reason": "一号两内容·乙", "ts": 4.0, "round": 2}}
    with open(jp, "a", encoding="utf-8", newline="\n") as f:
        for rec in (same, same, twin_a, twin_b):
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    L, e = _load_ok(snap, q1, "重复 nid 的 load")
    if e is None:
        nids = [n.get("nid") for c in L.nodes.values() for n in c]
        for nid in ("DUPA", "DUPB"):
            if nids.count(nid) > 1:
                q2.append(f"② 同一个号 {nid} 在链上占了 {nids.count(nid)} 节 —— "
                          "同一节记了两遍，认人索引从此含糊（谁是它？）")
        _book_ok(L, BASE_ROWS + 2, q2, "重复 nid")
        if _bad(L) > 0:
            q2.append(f"② 重复行被当成了坏行：bad_lines={_bad(L)}（重复不是坏，"
                      "身份认得出来就该去重）")
    _continue(snap, jp, BASE_ROWS + 2, q3, "重复 nid")
    return _res(q1, q2, q3, "DUPA 内容一样写两遍 + DUPB 同号不同内容各一条")


def run_base_newer(d):
    """⑧ 流水与底对不上（底比流水新）。"""
    q1, q2, q3 = [], [], []
    # (a) 底声称的消费位置超过流水实际大小
    snap, jp = _base(d, "t8a", n=5)
    with open(snap, encoding="utf-8") as f:
        j = json.load(f)
    j["_journal_upto"] = 10 ** 9
    with open(snap, "w", encoding="utf-8") as f:
        json.dump(j, f, ensure_ascii=False)
    L, e = _load_ok(snap, q1, "底偏移越界的 load")
    if e is None:
        _book_ok(L, 5, q2, "底偏移越界")
    _continue(snap, jp, 5, q3, "底偏移越界")
    # (b) 底里有流水里根本没有的节，流水被换成空的（旧代残留）
    snap2, jp2 = _base(d, "t8b", n=5)
    L0, e0 = _call(lambda: Ledger.load(snap2, writer="probe", empty_ok=True))
    if e0 is None:
        line, e1 = _call(lambda: L0.append(jp2, K, 1, "只在底里的那一节",
                                           "fp|base", writer="probe"))
        if e1 is None and line is not None:
            _call(lambda: L0.save(snap2))          # 底 6 节，流水还是 5 行
        with open(jp2, "w", encoding="utf-8") as f:
            f.write("")                            # 流水换成旧代（空的）
        L2, e2 = _load_ok(snap2, q1, "底比流水新（流水是空的）的 load")
        if e2 is None:
            _book_ok(L2, 6, q2, "底比流水新")
        _continue(snap2, jp2, 6, q3, "底比流水新")
    else:
        q1.append(f"① 前置的 load 就不动了：{_err(e0)}")
    return _res(q1, q2, q3, "_journal_upto 越界 + 底 6 节流水空")


CORPUS = {
    "truncated_json": ("① 截断的 JSON 行（半行/空行混杂）", run_truncated),
    "binary_garbage": ("② 二进制垃圾（\\x00、随机字节）", run_binary),
    "giant_line": ("③ 超长单行（≥10 MB 一行）", run_giant),
    "deep_nesting": ("④ 深嵌套 10⁵ 层", run_deep),
    "invalid_utf8": ("⑤ 非法 UTF-8 字节", run_invalid_utf8),
    "type_bomb": ("⑥ 字段类型炸弹", run_type_bomb),
    "duplicate_nid": ("⑦ 重复 nid 的两节", run_dup_nid),
    "base_newer_than_journal": ("⑧ 流水与底对不上（底比流水新）", run_base_newer),
}


# ══════════════ 子进程那一侧 ══════════════

def _child_run(cls, d):
    """子进程里跑一类：自己采样自己的 RSS 峰值（父进程跨进程读出来的数在这台机器
    上不可信 —— 实测父看子 3 MB、子自报 15.9 MB，差 5 倍，拿它当上限等于没有）。
    """
    import threading

    try:
        import psutil
        me = psutil.Process()
    except Exception:                             # noqa: BLE001 —— 量不到就算了，不是红
        me = None
    peak = {"mb": 0.0}
    stop = threading.Event()

    def _sample():
        while not stop.is_set():
            if me is not None:
                try:
                    peak["mb"] = max(peak["mb"], me.memory_info().rss / 1048576.0)
                except Exception:                 # noqa: BLE001
                    pass
            stop.wait(0.02)

    th = threading.Thread(target=_sample, daemon=True)
    th.start()
    t0 = time.perf_counter()
    try:
        out = CORPUS[cls][1](d)
    finally:
        stop.set()
        th.join(timeout=1)
    out["elapsed"] = time.perf_counter() - t0
    if me is not None:
        try:
            peak["mb"] = max(peak["mb"], me.memory_info().rss / 1048576.0)
        except Exception:                         # noqa: BLE001
            pass
    out["rss_mb"] = peak["mb"]
    return out


def _run_in_child(cls, d):
    """父进程起子进程跑一类：掐时间（挂了/超了当场 kill 判红）+ 核子进程自报的
    RSS 峰值（撑爆即红）。"""
    t0 = time.perf_counter()
    p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--run", cls, d],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=REPO)
    deadline = t0 + BUDGET_S
    while p.poll() is None:
        if time.perf_counter() > deadline:
            p.kill()
            p.wait()
            return {"dead": f"① 挂死：单类 {cls} 超过 {BUDGET_S}s 线，已 kill"}
        time.sleep(0.02)
    out, err = p.communicate(timeout=10)
    if p.returncode != 0:
        tail = err.decode("utf-8", "replace").strip().splitlines()[-6:]
        return {"dead": f"① 打死：子进程退出码 {p.returncode}（{cls}）：\n      "
                        + "\n      ".join(tail)}
    try:
        res = json.loads(out.decode("utf-8"))
    except Exception as e:                        # noqa: BLE001
        return {"dead": f"① 子进程没交出结果（{type(e).__name__}: {e}）："
                        f"{out[:300]!r} stderr={err[:300]!r}"}
    res["wall"] = time.perf_counter() - t0
    if res.get("rss_mb", 0) > MEM_CAP_MB:
        return {"dead": f"① 撑爆：子进程 RSS 峰值 {res['rss_mb']:.0f} MB "
                        f"> {MEM_CAP_MB} MB（单类 {cls}）"}
    return res


# ══════════════ 测试 ══════════════

def test_corpus_is_at_least_eight_classes():
    """任务书硬线：corpus ≥ 8 类。"""
    assert len(CORPUS) >= 8, f"corpus 只有 {len(CORPUS)} 类（该 ≥8）"
    assert len(set(CORPUS)) == len(CORPUS), "类名重了"


@pytest.mark.parametrize("cls", list(CORPUS))
def test_malformed_input_three_questions(cls, tmp_path):
    """每类三问：① 打死没有 ② 报得诚实吗 ③ 能继续用吗 —— 全绿才算过。"""
    what, _ = CORPUS[cls]
    res = _run_in_child(cls, str(tmp_path))
    errs = []
    if res.get("dead"):
        errs.append(res["dead"])
    else:
        for q in ("q1", "q2", "q3"):
            errs.extend(res.get(q) or [])
        if res.get("elapsed", 0) > BUDGET_S:
            errs.append(f"单类耗时 {res['elapsed']:.1f}s（> {BUDGET_S}s 线）")
    assert not errs, (f"【{cls}】{what} 三问没全过：\n" + "\n".join(errs[:20]))
    print(f"\n[malformed:{cls}] {what} · ①不打死 ②报得诚实 ③还能用 · "
          f"子进程 {res.get('elapsed', 0):.2f}s / 墙钟 {res.get('wall', 0):.2f}s · "
          f"RSS 峰值 {res.get('rss_mb', 0):.0f} MB · {res.get('note', '')}")


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--run":
        print(json.dumps(_child_run(sys.argv[2], sys.argv[3]),
                         ensure_ascii=False), end="")
        raise SystemExit(0)
    raise SystemExit("跑法：python -m pytest -q tests/test_malformed_input.py")

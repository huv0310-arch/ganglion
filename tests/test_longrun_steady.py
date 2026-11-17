# -*- coding: utf-8 -*-
"""长跑稳态压测（round 59）—— 500 轮 save / load / compact，四个量不许走样。

round 56/57/58 把**故障类**（真并行、崩溃、IO 错误）都验完了；这一份补最后一块空白：
这本账在生产里是「一直开着」的 —— 跑几千轮 append / save / compact / load 会不会
漏句柄、漏内存、攒垃圾文件、账漂移。**先测后修**：测出问题才动刀，测不出就交测量表。

N_ROUNDS = 500 轮，每轮 append 1–4 节 → 随机 save → 每 7 轮 compact → 每 10 轮
load（重开账本），每 100 轮一个检查点（失败信息带轮次）。四查：

  ① 句柄  Windows `psutil.Process().num_handles()`：循环前后不涨（`HANDLE_TOL` 的
             余量是给 gc / 句柄复用的，不是给泄漏的 —— 漏一个 fd 或线程句柄，
             500 轮下来就是几百个，当场红）
  ② 内存  `tracemalloc` 快照：第 1 轮之后 → 第 500 轮 Python 层净增 < `MEM_LIMIT_MB`
             （实测后写死；账本身按节线性长，这条线卡的是「毧行还长得快」的部分）
  ③ 垃圾  `.tmp` / `.lock` / **孤儿 `.compact`**（没登记进 `_compacted` 的底）= 0；
             `.compact` 总数 ≤ `KEEP_COMPACT_BACKUPS`（10 —— 保留策略本来就要留 10 份，
             留下的都得是登记过的，登记过 = load 读得到、`_prune_backups` 管得着）
  ④ 漂移  第 1 轮 load 出来的链、以及每个检查点 load 出来的链，对第 500 轮都得是
             **前缀逐字节一致**（后来的行只许追加，不许改写、不许重排、不许重号）

总时长 ≤ `BUDGET_S`（120 秒）。固定种子 `SEED = 20261107`（与 round 56/57/58 同号留锚），
全程 pytest 临时目录，真实账一个字节不碰。
"""
import gc
import json
import os
import random
import sys
import time
import tracemalloc

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psutil                                    # noqa: E402

from ledger import Ledger, addr                  # noqa: E402
from _invariants import chain_break_at, total_rows  # noqa: E402

SEED = 20261107           # 固定：换台机器同一条序列（与 round 56/57/58 同号留锚）
N_ROUNDS = 500            # 任务书的线：≥ 500 轮
CHECK_EVERY = 100         # 每 100 轮一个检查点
BUDGET_S = 120            # 任务书硬线：总时长 ≤ 120 秒
SAVE_P = 0.30             # 每轮 30% 概率随机 save
COMPACT_EVERY = 7         # 每隔几轮压实一次
LOAD_EVERY = 10           # 定期重开账本
N_ADDR = 8                # 8 个地址轮着写：链有深有浅，不是一条链刷到底
MEM_LIMIT_MB = 5.0        # 实测 500 轮净增 1.66 MB（≈1.1 KB/节，账本身按节线性长）→ 写死 5.0
HANDLE_TOL = 8            # 句柄余量：实测 500 轮 149→151（+2 是抖动，不是每轮涨）；给 gc 与句柄复用，不给泄漏

ADDRS = [addr(f"长跑稳态 地址{i}") for i in range(N_ADDR)]


# ════════════════ 现场的尺 ════════════════

def _chain_map(L):
    """把每条地址的链各自序列化成一串字节（圈号升序，每节一行 JSON）。

    漂移比的就是它：同一条链逐字节比前缀，谁改了旧节、谁重排了、谁重了号，
    前缀当场对不上。**不做成一本大账的全局序列** —— 全局按地址排序时，
    后来新增的地址会插进排序里，那是排序错位不是账漂移（第 1 轮 3 条地址、
    第 500 轮 8 条，全局前缀天然对不上）。
    """
    out = {}
    for key in sorted(L.nodes):
        out[key] = "\n".join(
            json.dumps(node, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"))
            for node in L.nodes[key]).encode("utf-8")
    return out


def _nids(L):
    return {n.get("nid") for c in L.nodes.values() for n in c}


def _junk(d):
    """(tmp, lock, compact) 三类残留文件清单。"""
    names = os.listdir(d)
    return (sorted(n for n in names if n.endswith(".tmp")),
            sorted(n for n in names if n.endswith(".lock")),
            sorted(n for n in names if n.endswith(".compact")))


def _where(want, got):
    """前缀对不上时，指到第一个不一致的字节（失败信息要能一眼看见漂在哪儿）。"""
    n = min(len(want), len(got))
    for i in range(n):
        if want[i] != got[i]:
            return (f"第 {i} 字节起不一致：基线 …{want[max(0, i - 20):i + 40]!r}"
                    f" vs 现在 …{got[max(0, i - 20):i + 40]!r}")
    return f"基线 {len(want)} 字节、现在 {len(got)} 字节（短了 = 旧节被抽走）"


def _drift(baseline, final, where="第 1 轮"):
    """旧账必须是新账的前缀：每条地址都得「前缀逐字节一致」，新地址不算漂。

    `where` 只是报错时说清这份旧账是哪个时点攒的（第 1 轮 / 第几个检查点）。
    """
    errs = []
    for key, want in baseline.items():
        got = final.get(key)
        if got is None:
            errs.append(f"[{where}] 地址 {key} 到第 {N_ROUNDS} 轮整条没了（旧节被抽走）")
        elif not got.startswith(want):
            errs.append(f"[{where}] 地址 {key} 漂了：{_where(want, got)}")
    return errs


# ════════════════ 测试 ════════════════

def test_longrun_steady_state_four_checks(tmp_path):
    """500 轮长跑：句柄 / 内存 / 垃圾 / 漂移四查全绿，且在 120 秒线内。"""
    t0 = time.perf_counter()
    d = str(tmp_path)
    snap = os.path.join(d, "book.json")
    jp = snap + ".jsonl"
    rng = random.Random(SEED)                      # 固定种子：换台机器同一条序列
    proc = psutil.Process()
    expected = set()                               # append 过的 nid（行数守恒的口径）

    tracemalloc.start()
    L = Ledger(writer="longrun", ttl=None)

    # ── 第 1 轮：落几节 → save → load，把「第 1 轮 load 的链」钉成基线 ──
    for j in range(3):
        line = L.append(jp, ADDRS[j % N_ADDR], j % 2, reason=f"基线第{j + 1}节",
                        ctx_fp=f"fp|{j % 3}", writer="longrun")
        assert line is not None, "基线落账被准入闸拦下了（口径不该变）"
        expected.add(line["node"]["nid"])
    L.save(snap)
    L = Ledger.load(snap, journal=jp, writer="longrun", empty_ok=True)
    baseline = _chain_map(L)
    assert baseline and sum(len(v.split(b"\n")) for v in baseline.values()) == 3, \
        "第 1 轮 load 出来不是那 3 节基线：这一轮的基线立不住，这轮白跑"
    assert not chain_break_at(L), f"第 1 轮基线链就断了：{chain_break_at(L)}"
    gc.collect()
    mem0, _ = tracemalloc.get_traced_memory()
    h0 = proc.num_handles()

    marks = []                                     # 每个检查点的实测数（README 要用）
    history = []                                   # 每个检查点的链快照（漂移要逐份比前缀）
    compacted = 0
    for i in range(1, N_ROUNDS + 1):
        # ① 每轮 append 几节（1–4 节、8 个地址轮着写）
        for _ in range(rng.randint(1, 4)):
            k = rng.choice(ADDRS)
            line = L.append(jp, k, rng.randrange(2), reason=f"第{i}轮",
                            ctx_fp=f"fp|{i % 5}", writer="longrun")
            assert line is not None, f"第 {i} 轮 append 被准入闸拦下了（口径不该变）"
            expected.add(line["node"]["nid"])
        # ② 随机 save
        if rng.random() < SAVE_P:
            L.save(snap)
        # ③ 每隔几轮 compact
        if i % COMPACT_EVERY == 0:
            ret = L.compact(snap, journal=jp)
            assert ret is not False, f"第 {i} 轮压实让了路（单进程没有争用，不该让）"
            if isinstance(ret, str):
                compacted += 1
        # ④ 定期 load（重开账本）
        if i % LOAD_EVERY == 0:
            L = Ledger.load(snap, journal=jp, writer="longrun", empty_ok=True)

        # ── 每 100 轮一个检查点，失败信息带轮次 ──
        if i % CHECK_EVERY == 0:
            gc.collect()
            cur, _peak = tracemalloc.get_traced_memory()
            h = proc.num_handles()
            tmp, lock, compact = _junk(d)
            orphans = [n for n in compact
                       if n not in getattr(L, "_compacted", [])]
            errs = []
            if h > h0 + HANDLE_TOL:
                errs.append(f"句柄从 {h0} 涨到 {h}（+{h - h0} > {HANDLE_TOL}）")
            if tmp:
                errs.append(f"留了 .tmp {tmp[:3]}")
            if lock:
                errs.append(f"留了 .lock {lock[:3]}")
            if orphans:
                errs.append(f"孤儿 .compact {orphans[:3]}（没登记，load / prune 都管不着）")
            if len(compact) > Ledger.KEEP_COMPACT_BACKUPS:
                errs.append(f".compact {len(compact)} 份 > 保留上限 "
                            f"{Ledger.KEEP_COMPACT_BACKUPS}")
            if _nids(L) != expected or total_rows(L) != len(expected):
                errs.append(f"行数不守恒：链上 {total_rows(L)} 节 / "
                            f"nid {len(_nids(L))} 个，落账 {len(expected)} 节")
            broke = chain_break_at(L)
            if broke:
                errs.append(f"链断了：{broke}")
            bad = int(L.stat.get("bad_lines") or 0)
            if bad:
                errs.append(f"坏行进了账：bad_lines={bad}")
            assert not errs, (f"第 {i} 轮检查点没过：\n" + "\n".join(errs))
            history.append((i, _chain_map(L)))
            marks.append({"round": i, "handles": h,
                          "mem_mb": round((cur - mem0) / 1e6, 2),
                          "tmp": len(tmp), "lock": len(lock),
                          "compact": len(compact), "rows": total_rows(L)})

    # ── ④ 漂移：第 1 轮 load 的链 + 每个检查点的链，对第 N 轮都得是前缀逐字节 ──
    final_chain = _chain_map(L)
    drift = _drift(baseline, final_chain, "第 1 轮")
    for i, snap_chain in history:
        drift += _drift(snap_chain, final_chain, f"第 {i} 轮检查点")
    assert not drift, \
        (f"账漂移了（旧账不是第 {N_ROUNDS} 轮链的前缀）：\n" + "\n".join(drift))
    base_bytes = sum(len(v) for v in baseline.values())
    final_bytes = sum(len(final_chain[k]) for k in baseline)
    n_hist = len(history)

    # ── ② 内存：净增 < 写死的那条线 ──
    # 量之前先把**测试自己**攒的链快照放下（history 是尺，不是被测代码的账，
    # 留着量会把尺的重量算进被测代码头上）。
    del history, final_chain
    gc.collect()
    mem1, peak = tracemalloc.get_traced_memory()
    delta_mb = (mem1 - mem0) / 1e6
    assert delta_mb < MEM_LIMIT_MB, \
        (f"{N_ROUNDS} 轮后 tracemalloc 净增 {delta_mb:.2f} MB ≥ {MEM_LIMIT_MB} MB"
         f"（峰值 {peak / 1e6:.2f} MB）")

    # ── 收尾：账还开着，该没的都没了，账本该记的都记着 ──
    tmp, lock, compact = _junk(d)
    assert not tmp and not lock, f"收尾还有残留：tmp={tmp} lock={lock}"
    assert len(compact) <= Ledger.KEEP_COMPACT_BACKUPS, \
        f"收尾 .compact {len(compact)} 份 > 保留上限 {Ledger.KEEP_COMPACT_BACKUPS}"
    assert L.stat.get("compact_skipped", 0) == 0, \
        f"压实静默让路 {L.stat.get('compact_skipped')} 次（单进程没争用）"
    assert compacted >= N_ROUNDS // COMPACT_EVERY - 2, \
        f"只真压实了 {compacted} 次（该有约 {N_ROUNDS // COMPACT_EVERY} 次）"
    took = time.perf_counter() - t0
    assert took <= BUDGET_S, f"{N_ROUNDS} 轮跑了 {took:.1f}s（> {BUDGET_S}s 线）"

    h1 = proc.num_handles()
    assert h1 <= h0 + HANDLE_TOL, f"句柄从 {h0} 涨到 {h1}（+{h1 - h0}）"

    print(f"\n[longrun] {N_ROUNDS} 轮 · 压实 {compacted} 次 · 落账 {len(expected)} 节 · "
          f"检查点 " + " ".join(f"{m['round']}→句柄{m['handles']}/"
                                f"内存+{m['mem_mb']}MB/垃圾{m['tmp'] + m['lock']}+"
                                f"底{m['compact']}" for m in marks) +
          f" · 句柄 {h0}→{h1} · tracemalloc 净增 {delta_mb:.2f} MB"
          f"（峰值 {peak / 1e6:.2f} MB）· tmp/lock/孤儿底 0 · "
          f"前缀逐字节：第 1 轮 3 条地址 {base_bytes}→{final_bytes} 字节"
          f" + {n_hist} 个检查点全一致 · 快照 {os.path.getsize(snap) / 1e6:.2f} MB"
          f"（底名单 {len(L._compacted)} 条）· {took:.1f}s")

    tracemalloc.stop()


def test_longrun_steady_state_runs_alone_from_cli(tmp_path):
    """这条长跑自己能单独跑（README/任务书让人直接敲它），小档也要全绿。

    `LONGLRUN_ROUNDS` 只是给冒烟用的旋钮（默认 120 轮），正本仍是上面那条 500 轮 ——
    改旋钮不改测试文件，跑法一行：`LONGLRUN_ROUNDS=120 python -m pytest -q -s tests/test_longrun_steady.py`
    """
    import subprocess
    rounds = os.environ.get("LONGLRUN_ROUNDS", "120")
    probe = (
        "import os, sys, pathlib, tempfile;"
        "sys.path.insert(0, os.path.join(os.getcwd(), 'tests'));"
        "sys.path.insert(0, os.getcwd());"
        "import test_longrun_steady as t;"
        "t.N_ROUNDS = int(os.environ['LONGLRUN_ROUNDS']);"
        "t.CHECK_EVERY = 40;"
        "t.test_longrun_steady_state_four_checks(pathlib.Path(tempfile.mkdtemp()));"
        "print('CLI-OK')"
    )
    p = subprocess.run([sys.executable, "-c", probe],
                       capture_output=True, text=True, cwd=REPO,
                       timeout=BUDGET_S + 60,
                       env=dict(os.environ, LONGLRUN_ROUNDS=rounds))
    assert p.returncode == 0, f"rc={p.returncode}\n{p.stdout[-4000:]}\n{p.stderr[-4000:]}"
    assert "CLI-OK" in p.stdout and "[longrun]" in p.stdout


if __name__ == "__main__":
    raise SystemExit("跑法：python -m pytest -q -s tests/test_longrun_steady.py")

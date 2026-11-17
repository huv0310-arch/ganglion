# -*- coding: utf-8 -*-
"""多进程并发压测（round 56）—— 真并行打一本账，不变量一条不许破。

round 56 任务书第 1 条：锁协议此前是「设计上互斥、测试上单进程」——
并发正确性从没被**真并行**验证过。这一份用 spawn 出来的真进程（不是线程）
混合打：6 进程 × 每进程 80 次随机操作（append / 墓碑 / compact / recent），
全跑在 pytest 临时目录里，真实账一个字节不碰。

不变量（逐条落断言）：
  ① 链校验    终账 `chain_break_at(L) is None`，各进程收尾内存里也查一遍。
  ② 零丢失    各进程 append 落下的 nid 集合 == 终账链上 nid 集合
               （不多、不少、不重复 —— 丢一行、多一行、复制一行都算破）。
  ③ 无半行    流水 / 压实底 / 归档**每一行都 parse 得动**，流水尾部必须有换行；
               快照 json.load 得动（交错行、半行都当场红）。
  ④ 锁不残留  跑完工作目录里 `*.lock` 一个不剩（含 `.compact.lock`）。
  ⑤ recent 自洽 终账 recent(n) 与独立口径逐节对上（n 取 0/1/7/40）。
  ⑥ 墓碑不凭空 链上每颗墓碑：真 append 过、且真有人扫过它；
               扫过却在终账上复活的节单独计数（复活 = 丢刀，见 ⑦）。
  ⑦ 压实底可读  每份 `.compact` 备份逐行 parse 得动。

Windows / spawn 怎么写（任务书点名要写清楚）：
  · `multiprocessing.get_context("spawn")` —— Windows 默认就是 spawn；
    spawn 会把子进程的 `__main__` 重新 import 一遍，所以**独立跑这个文件**
    必须有 `if __name__ == "__main__": mp.freeze_support()` 门（普通 python
    下 freeze_support 是 no-op，打成 exe 时才起作用），门里的测试代码
    子进程 import 时不会执行（子进程拿到的模块名不是 `__main__`）。
  · 工作函数 `_worker` 必须是**模块级**函数（要能按名字 pickle 进子进程），
    子进程 import 本模块时只跑模块级定义、不跑测试。
  · 固定种子 `SEED`：换台机器跑出同一条操作序列，可复现。
"""
import itertools
import json
import multiprocessing as mp
import os
import random
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ledger import Ledger, addr                # noqa: E402
from _invariants import (chain_break_at, expected_recent,  # noqa: E402
                         recent_matches, total_rows)

SEED = 20261107            # 固定：换台机器同一条序列
PROCS = 6                  # 任务书的线：4–8 进程
OPS_PER_PROC = 80          # 任务书的线：每进程 50–200 次
BUDGET_S = 60              # 任务书的硬线：整份 ≤ 60 秒
JOIN_DEADLINE_S = 55       # 给子进程收尾留 5 秒余量

# 操作配比：写多（append 是本职），刀与查询各占一点
OPS = [("append", 45), ("tombstone", 20), ("compact", 15), ("recent", 20)]
NAMES = [n for n, _ in OPS]
WEIGHTS = [w for _, w in OPS]

KEYS = [addr(f"压力-{j}") for j in range(12)]   # 所有进程共用同一批地址（真抢链）
BASE_TS = 1_700_000_000
SWEEP_NOW = BASE_TS + 10 ** 6                  # 扫钟：带 ttl 的节全过窗


def _worker(idx, workdir, seed, n_ops, q):
    """一个进程的整场：随机操作到收尾，把账交给主进程判。"""
    out = {"idx": idx, "appended": [], "swept": [], "ops": {}, "errors": [],
           "recent_checks": 0, "chain_err": None}
    snap = os.path.join(workdir, "book.json")
    jrn = snap + ".jsonl"
    arch = os.path.join(workdir, f"gone{idx}.jsonl")
    try:
        rng = random.Random(seed + idx)
        clock = itertools.count(BASE_TS + idx * 1000)
        L = Ledger(writer=f"w{idx}", ttl=None, now=lambda: float(next(clock)))
        for step in range(n_ops):
            op = rng.choices(NAMES, weights=WEIGHTS, k=1)[0]
            out["ops"][op] = out["ops"].get(op, 0) + 1
            try:
                if op == "append":
                    line = L.append(jrn, rng.choice(KEYS), rng.choice((0, 1)),
                                    reason=f"p{idx}-第{step}步",
                                    ctx_fp=f"fp|{rng.randrange(5)}",
                                    ttl=(60 if rng.random() < 0.5 else None),
                                    writer=f"w{idx}")
                    if line is not None:            # 准入闸拦下就不算落账
                        out["appended"].append(line["node"]["nid"])
                elif op == "tombstone":
                    before = {n.get("nid") for c in L.nodes.values()
                              for n in c if L.is_tombstone(n)}
                    L.sweep_ttl(now=SWEEP_NOW, archive=arch)
                    after = {n.get("nid") for c in L.nodes.values()
                             for n in c if L.is_tombstone(n)}
                    out["swept"] = list(set(out["swept"]) | (after - before))
                    L.save(snap)                   # 墓碑只在快照里，要存才落盘
                elif op == "compact":
                    L.compact(snap, journal=jrn)   # 拿不到锁就让路（返回 False），不报错
                else:
                    n = rng.choice((-3, 0, 1, 7, 40))
                    out["recent_checks"] += 1
                    if not recent_matches(L, n):
                        out["errors"].append(
                            f"第{step}步 recent({n}) 与独立口径不一致（进程内自洽破了）")
            except Exception as e:                  # noqa: BLE001 —— 操作级失败也算红
                out["errors"].append(f"第{step}步 {op}: {type(e).__name__}: {e}")
        broke = chain_break_at(L)
        if broke:
            out["chain_err"] = f"进程{idx} 收尾内存里链断了：{broke}"
    except BaseException as e:                      # noqa: BLE001 —— 崩了也要交尸
        out["errors"].append(f"worker崩了 {type(e).__name__}: {e}")
    finally:
        q.put(out)


def _readable_lines(path):
    """文件里非空行逐行 parse；返回 (总行数, 坏行清单)。"""
    bad, n = [], 0
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        return 0, [f"{path} 读不动：{e}"]
    for i, ln in enumerate(raw.split(b"\n"), 1):
        if not ln.strip():
            continue
        n += 1
        try:
            json.loads(ln.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            bad.append(f"{os.path.basename(path)} 第{i}行 parse 不动（{type(e).__name__}: {e}）"
                       f"：{ln[:60]!r}")
    return n, bad


def run_stress(workdir, procs=PROCS, ops=OPS_PER_PROC, seed=SEED):
    """跑一轮并发压测，返回汇总（测试与命令行入口共用）。"""
    workdir = str(workdir)
    os.makedirs(workdir, exist_ok=True)
    ctx = mp.get_context("spawn")                  # Windows 默认也是 spawn，写死它
    q = ctx.Queue()
    t0 = time.perf_counter()
    ps = [ctx.Process(target=_worker,
                      args=(i, workdir, seed, ops, q),
                      name=f"stress-{i}")
          for i in range(procs)]
    for p in ps:
        p.start()
    deadline = t0 + JOIN_DEADLINE_S
    results, hung = [], []
    for p in ps:
        p.join(timeout=max(1.0, deadline - time.perf_counter()))
        if p.is_alive():
            hung.append(p.name)
            p.terminate()
            p.join(timeout=5)
    while len(results) < len(ps):
        try:
            results.append(q.get(timeout=5))
        except Exception:
            break
    wall = time.perf_counter() - t0

    snap = os.path.join(workdir, "book.json")
    jrn = snap + ".jsonl"
    # 终账：所有进程都退干净了再读，这是权威口径
    L = Ledger.load(snap, journal=jrn, writer="chk", empty_ok=True)

    appended, swept, errors = set(), set(), []
    for r in results:
        appended.update(r["appended"])
        swept.update(r["swept"])
        errors += [f"进程{r['idx']}: {e}" for e in r["errors"]]
        if r.get("chain_err"):
            errors.append(r["chain_err"])
    if hung:
        errors.append(f"进程卡死超时被杀：{hung}")
    if len(results) != len(ps):
        errors.append(f"只收到 {len(results)}/{len(ps)} 个进程的作业")

    # ③ 无半行：流水尾部要有换行，所有行 parse 得动
    line_bad, line_n = [], 0
    if os.path.exists(jrn) and os.path.getsize(jrn):
        with open(jrn, "rb") as f:
            f.seek(-1, 2)
            if f.read(1) != b"\n":
                line_bad.append("流水最后一行没有换行符（半行/被截断）")
    for name in sorted(os.listdir(workdir)):
        p = os.path.join(workdir, name)
        if not os.path.isfile(p):
            continue
        if name.endswith(".jsonl") or ".jsonl." in name or name.endswith(".compact"):
            n, bad = _readable_lines(p)
            line_n += n
            line_bad += bad
    try:
        with open(snap, encoding="utf-8") as f:
            json.load(f)
    except FileNotFoundError:
        pass
    except Exception as e:                          # noqa: BLE001
        line_bad.append(f"快照读不动：{type(e).__name__}: {e}")

    # ④ 锁不残留
    locks = sorted(n for n in os.listdir(workdir) if n.endswith(".lock"))

    # ⑥ 墓碑不凭空 + 扫过复活的节（复活 = 刀白扫，单独量出来）
    tomb = {n.get("nid") for c in L.nodes.values() for n in c if L.is_tombstone(n)}
    resurrected = sorted(swept - tomb)
    ghost_tomb = sorted(tomb - appended)
    unswept_tomb = sorted(tomb - swept)

    # ⑤ recent 自洽（终账）
    recent_bad = [n for n in (0, 1, 7, 40) if not recent_matches(L, n)]

    # ① 链校验（终账）
    broke = chain_break_at(L)

    # ② 零丢失：链上 nid 集合 == 各进程 append 的 nid 集合
    chain_nids = [n.get("nid") for c in L.nodes.values() for n in c]
    dup = len(chain_nids) - len(set(chain_nids))
    rows = total_rows(L)

    errs = list(errors)
    if broke:
        errs.append(f"① 链断了：{broke}")
    if rows != len(appended) or set(chain_nids) != appended:
        missing = sorted(appended - set(chain_nids))[:5]
        extra = sorted(set(chain_nids) - appended)[:5]
        errs.append(f"② 零丢失破了：链上 {rows} 节 != 各进程 append {len(appended)} 节"
                    f"；丢掉的样例 {missing}；多出来的样例 {extra}")
    if dup:
        errs.append(f"② 链上有 {dup} 个重复 nid（同一节被记了两次）")
    if line_bad:
        errs.append("③ " + "；".join(line_bad[:5]))
    if locks:
        errs.append(f"④ 锁残留：{locks}")
    if recent_bad:
        errs.append(f"⑤ 终账 recent({recent_bad}) 与独立口径不一致")
    if ghost_tomb:
        errs.append(f"⑥ 凭空的墓碑（没 append 过）：{ghost_tomb[:5]}")
    if unswept_tomb:
        errs.append(f"⑥ 没人扫过却成了墓碑：{unswept_tomb[:5]}")
    if resurrected:
        errs.append(f"⑦ 扫成墓碑又在终账上复活（丢刀）{len(resurrected)} 节，"
                    f"样例 {resurrected[:5]}")

    return {"errors": errs, "wall": wall, "rows": rows, "appended": len(appended),
            "tomb": len(tomb), "swept": len(swept), "resurrected": len(resurrected),
            "line_n": line_n, "procs": procs, "ops_per_proc": ops,
            "op_mix": {n: sum(r["ops"].get(n, 0) for r in results) for n in NAMES},
            "recent_checks": sum(r["recent_checks"] for r in results)}


def _main():
    """命令行入口：当探针单独跑（python tests/test_concurrency_mp.py）。"""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="mp_stress_") as d:
        r = run_stress(d)
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 1 if r["errors"] else 0


if __name__ == "__main__":
    # Windows spawn：子进程会重新 import 本文件，这道门是必须的（freeze_support
    # 在普通 python 下是 no-op，打成 exe 时才生效；门里的代码子进程不会跑）。
    mp.freeze_support()
    raise SystemExit(_main())


def test_stale_saver_cannot_resurrect_a_tombstone(tmp_path):
    """根因三的钉子（round 56）：A 扫成墓碑落盘，B 拿扫之前的旧副本存盘，不许复活。

    墓碑只在快照里（`sweep_ttl` 明写"绝不碰 journal"），所以丢刀的唯一现场就是
    `_merge_from`：同一节（同 nid）两边都在时，以前"我这份赢"，于是旧副本把刀抹了。
    单进程就能复现（多进程压测里约 1/12 抓到），所以这条不 spawn 进程，稳。
    """
    p = str(tmp_path / "book.json")
    jp = p + ".jsonl"
    k = addr("同一件事")

    a = Ledger(writer="a", ttl=None)
    a.append(jp, k, 1, "判对了", "fp|A", ttl=60)
    a.save(p)
    b = Ledger.load(p, writer="b", ttl=None)      # B 在 A 扫之前读进内存（这一节是活的）

    a.sweep_ttl(now=10 ** 12, archive=str(tmp_path / "gone.jsonl"))
    a.save(p)
    assert Ledger.load(p, writer="c").nodes[k][0].get("tombstone"), "前提：A 的墓碑已落盘"

    b.save(p)                                      # B 用自己的旧内存存盘
    after = Ledger.load(p, writer="c").nodes[k][0]
    assert after.get("tombstone"), "旧副本把别人刚扫的墓碑抹回活的了（丢刀）"


def test_merge_adopts_disk_round_for_a_shared_node(tmp_path):
    """根因二的钉子（round 56）：同一节两边圈号不同，合并后一条链上不许出现两个 1 圈。

    现场：我先记了 Y（我这儿 1 圈），盘上 Y 已被重演成 2 圈、盘上还有别人的 X 占着
    1 圈 —— X 并进来、Y 又不让号，链上就两个 1 圈，`chain_break_at` 当场断。
    盘上那节是锚（既定口径「号一个字不动」），锚的号也以盘上为准。
    """
    p = str(tmp_path / "book.json")
    jp = p + ".jsonl"
    k = addr("同一条链")

    disk = Ledger(writer="disk", ttl=None)
    disk.append(jp, addr("别人的 X"), 1, "X", "fp|X")
    disk.append(jp, k, 0, "Y-盘上第2圈", "fp|Y")
    disk.save(p)

    mine = Ledger(writer="mine", ttl=None)         # 我这边：Y 是我自己记的，1 圈
    mine.append(jp, k, 0, "Y-我这儿第1圈", "fp|Y2")
    mine.save(p)                                    # 存盘前先并盘上那份

    broke = chain_break_at(mine)
    assert broke is None, f"合并后链断了：{broke}"
    rounds = [n.get("round") for n in mine.nodes[k]]
    assert rounds == [1, 2], f"圈号该是 1..n 连号，收到 {rounds}"


def test_multiprocess_random_ops_keep_invariants(tmp_path):
    """6 进程 × 80 次随机操作（append/墓碑/压实/时间线）真并行：七条不变量全真。"""
    t0 = time.perf_counter()
    r = run_stress(str(tmp_path))
    took = time.perf_counter() - t0

    assert r["ops_per_proc"] >= 50 and r["ops_per_proc"] <= 200, "每进程步数不在 50–200 线内"
    assert 4 <= r["procs"] <= 8, "进程数不在 4–8 线内"
    assert not r["errors"], ("不变量被打破了：\n" + "\n".join(r["errors"][:20]))
    # 四种操作都真走到了（一条都没抽到 = 这场压测没压到那个面）
    for n in NAMES:
        assert r["op_mix"].get(n, 0) > 0, f"整场一次 {n} 都没抽到：{r['op_mix']}"
    assert r["appended"] > 0 and r["recent_checks"] > 0, "一场空账，测了个寂寞"

    print(f"\n[mp] {r['procs']} 进程 × {r['ops_per_proc']} 步（{r['op_mix']}）· "
          f"append {r['appended']} 节 · 墓碑 {r['tomb']} 节（扫过 {r['swept']}，"
          f"复活 {r['resurrected']}）· 流水/底/归档 {r['line_n']} 行逐行 parse · "
          f"锁残留 0 · 墙钟 {took:.1f}s")

    assert took <= BUDGET_S, (
        f"整场 {took:.1f}s（> {BUDGET_S}s 线）——步数或进程数得往回收")

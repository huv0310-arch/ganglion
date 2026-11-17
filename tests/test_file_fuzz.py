# -*- coding: utf-8 -*-
"""文件损坏 fuzz（round 61）—— 随机字节 / 截断 / 拼接打在真实账本文件上，五不变量兜底。

round 60 的损坏语料是 8 个**手写**样本（`tests/test_malformed_input.py`）；这一份把「损坏」
变成随机生成的规模化模糊：固定 **12 个种子**，每种子先用正常操作攒一本小真账
（流水 + 快照 + 一份压实底），再随机选 1–3 种手法打在**流水和快照上各来一遍**。

五种手法（techs）：

  flip       随机字节翻转（1–6 处，各异或一个随机位）
  truncate   随机位置截断（只留前缀）
  move       随机切一段挪到别处（段长 ≤ 1/4）
  splice     头尾拼接两份文件（拿第二本真账的同名文件来拼）
  drop       抽掉中间若干字节（段长 ≤ 1/3）

五条不变量（任一违反即红）：

  ① 不打死   `load` 不许未捕获异常冒泡 —— RecursionError / UnicodeDecodeError /
             AttributeError / JSONDecodeError 一律收进「带原因、点名文件的拒绝」或坏行
  ② 不挂死   单种子 10s 线（子进程掐表，挂了 / 超了当场 kill 判红），全文件 ≤ 120s
  ③ 不假绿   处理完必须给出诚实读数：
             · `bad_lines` 盖得住**独立数出来的**「确实解不动的行」（不拿被测代码给自己作证）
             · 拒绝必须点名是哪个文件
             · 行数只少不多（上界 = 原账节数 + 拼接带进来的节）
             · 少的能说出少哪：不在场的行 + 计了数的坏行 + 收下的快照里被抽掉的节
  ④ 还能用   处理后同目录 append → 重读：链校验过、行数守恒、recent 与独立口径一致
  ⑤ 不动原文件 `load` 一个字节都不许改（测试自己才是破坏者）

**护栏自检**（防尺子睡着，round 60 的手法）：八把尺各喂一份**故意打坏**的输入，
确认都会真响；再各喂一份干净输入，确认不会永远红。

固定种子 `SEEDS`（12 个，**按覆盖面挑过**：五种手法各 ≥2 次、「快照被收下 / 被拒 /
流水里有坏行 / 没坏行」四条路各 ≥2 个种子）；其中 8 / 14 / 15 / 44 / 56 / 79 / 89
在本轮**修前**的扫描里红过，改前用这份测试在临时仓复跑实红的是 8 / 15 / 56 / 79 ——
账里的 uuid 与时间戳每次都不同，红哪几个种子会漂，所以**覆盖面那几条断言才是钉子**，
具体哪个种子红不是。每种子都在**子进程**里跑，父进程掐表。
单种子跑法（排障用）：`python tests/test_file_fuzz.py --run <seed> <目录>`。
全程 pytest 临时目录，真实账一个字节不碰。
"""
import json
import os
import random
import shutil
import subprocess
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ledger import Ledger, addr                            # noqa: E402
from _invariants import chain_break_at, recent_matches, total_rows  # noqa: E402

SEEDS = [8, 14, 15, 22, 44, 56, 62, 72, 79, 89, 99, 165]   # 固定：换台机器同一条垃圾序列
TECHS = ("flip", "truncate", "move", "splice", "drop")
BUDGET_S = 10.0                    # 任务书硬线：单种子 ≤ 10s
TOTAL_BUDGET_S = 120.0             # 任务书硬线：全文件 ≤ 120s
N = 3                              # 每批 3 节（原账 3 + 3 = 6 节）
KA = [addr(f"fuzz甲-{i}") for i in range(N)]     # 主账的三把钥匙
KB = [addr(f"fuzz乙-{i}") for i in range(N)]     # 拼接时用的第二本账
T0 = time.perf_counter()           # 全文件掐表（collection 起算）
_RESULTS = {}                      # 种子 → 结果（覆盖度那条测试用，跑过就不再跑一遍）


# ══════════════ 攒真账 ══════════════

def build(d, tag, keys, n=N):
    """一本来路清白的小真账：n 节 → 压实（存快照 + 留底 + 截空流水）→ 再存一次 → 再 n 节。

    再存那一次是关键：压实存的快照里 `_journal_upto` 还指着**旧流水**的长度，
    截空之后再存一次才归 0 —— 于是新流水从头读，随机打在流水头上的字节也躲不掉。
    底（`.compact`）与快照同源：快照被拒时底会整份重读，那几节补得回来。
    """
    sub = os.path.join(d, tag)
    os.makedirs(sub, exist_ok=True)
    snap = os.path.join(sub, "book.json")
    jp = snap + ".jsonl"
    L = Ledger(writer="fuzz", ttl=None)
    for i in range(n):
        line = L.append(jp, keys[i % len(keys)], 1 if i % 2 else 0,
                        reason=f"{tag}第{i + 1}节", ctx_fp=f"fp|{tag}|{i}",
                        writer="fuzz")
        assert line is not None, "前置落账被准入闸拦下了（口径不该变）"
    L.compact(snap)                # 存快照(n 节) + 留底 + 截空流水
    L.save(snap)                   # 截空之后再存一次：_journal_upto 归 0
    for i in range(n, 2 * n):
        line = L.append(jp, keys[i % len(keys)], 1 if i % 2 else 0,
                        reason=f"{tag}第{i + 1}节", ctx_fp=f"fp|{tag}|{i}",
                        writer="fuzz")
        assert line is not None, "前置落账被准入闸拦下了（口径不该变）"
    return snap, jp


def plan(seed):
    """种子的第一步：选 1–3 种手法（与 run_seed 同一个 rng、同一串取数）。"""
    rng = random.Random(seed)
    techs = rng.sample(list(TECHS), rng.randint(1, 3))
    return rng, techs


# ══════════════ 独立数数（不调被测代码） ══════════════

def snapshot_nodes(path):
    """快照里物理上有几节（读不动给 -1）。"""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return sum(len(c or ()) for c in (d.get("nodes") or {}).values())
    except Exception:                                  # noqa: BLE001 —— 读不动就是 -1
        return -1


def definitely_bad_lines(jp, upto):
    """独立数一遍「这一行确实解不动」：strict 解码失败 / 不是 JSON / 整行不是对象。

    只数这几类是**下界**：这三类在账本那边一律进 bad_lines，所以
    `账本的 bad_lines >= 这个数` 必须成立 —— 小了就是吞了坏行假装读全了。
    """
    try:
        with open(jp, "rb") as f:
            raw = f.read()
    except OSError:
        return 0
    start = upto if isinstance(upto, int) and 0 <= upto <= len(raw) else 0
    cut = raw.rfind(b"\n") + 1                         # 只数完整行（与 _absorb 同口径）
    if cut <= start:
        return 0
    bad = 0
    for piece in raw[start:cut].split(b"\n"):
        if not piece.strip():
            continue
        try:
            rec = json.loads(piece.decode("utf-8"))
        except Exception:                              # noqa: BLE001
            bad += 1
            continue
        if not isinstance(rec, dict):
            bad += 1
    return bad


# ══════════════ 五种手法 ══════════════

def corrupt(data, rng, techs, other):
    """按种子给定的顺序把手法依次打在一份文件上。"""
    out = bytearray(data)
    for t in techs:
        if t == "flip":
            if not out:
                continue
            for _ in range(rng.randint(1, 6)):
                out[rng.randrange(len(out))] ^= 1 << rng.randrange(8)
        elif t == "truncate":
            if len(out) > 1:
                del out[rng.randrange(len(out)):]
        elif t == "move":
            if len(out) > 8:
                a = rng.randrange(len(out) - 1)
                b = min(len(out), a + rng.randint(1, max(1, len(out) // 4)))
                seg = bytes(out[a:b])
                del out[a:b]
                p = rng.randrange(len(out) + 1)
                out[p:p] = seg
        elif t == "drop":
            if len(out) > 8:
                a = rng.randrange(len(out) - 1)
                b = min(len(out), a + rng.randint(1, max(1, len(out) // 3)))
                del out[a:b]
        elif t == "splice" and other:
            p = rng.randrange(len(out)) if out else 0
            q = rng.randrange(len(other)) if other else 0
            out = bytearray(bytes(out[:p]) + bytes(other[q:]))
    return bytes(out)


# ══════════════ 尺子（纯函数：护栏自检直接喂坏输入） ══════════════

def check_refusal(err, path):
    """① 抛出来的异常必须是「带原因、点名文件」的拒绝 —— 别的异常就是没接住。"""
    if err is None:
        return []
    errs = []
    if not isinstance(err, ValueError):
        errs.append(f"① 没收进拒绝的异常：{type(err).__name__}: {err}")
    msg = str(err).strip()
    if not msg:
        errs.append(f"① 拒绝了却一句原因都没有（{type(err).__name__}）")
    elif os.path.basename(path) not in msg:
        errs.append(f"① 拒绝了但没点名是哪个文件：{type(err).__name__}: {msg[:160]}")
    return errs


def check_time(elapsed, budget=BUDGET_S, what="单种子"):
    """② 掐表：挂死/太慢即红。"""
    return [f"② 挂死：{what}耗时 {elapsed:.2f}s > {budget}s 线"] if elapsed > budget else []


def check_bytes_unchanged(before, after, what="load"):
    """⑤ 原文件一字节不许动（测试自己才是破坏者）。"""
    return [f"⑤ {what} 动了原文件的字节（不许静默改用户的账）"] if before != after else []


def check_rows_bound(rows, bound, where="载入"):
    """③ 行数只少不多：不许凭空多出文件里没有的节。"""
    if rows > bound:
        return [f"③ 行数变多（{where}）：{rows} > 上限 {bound}"]
    return []


def check_honest(bad, dbad):
    """③ 不许假绿：坏行数必须盖得住独立数出来的「确实解不动的行」。"""
    if bad < dbad:
        return [f"③ 假绿：bad_lines={bad} < 独立数出来的解不动行 {dbad}"]
    return []


def check_missing(missing, absent, bad, snap_gone):
    """③ 少的能说出少哪：不在场的行 + 计了数的坏行 + 收下的快照里被抽掉的节。"""
    if missing <= 0:
        return []
    total = absent + bad + snap_gone
    if missing > total:
        return [f"③ 少了 {missing} 节说不出来（不在场的行 {absent} + 计了数的坏行 {bad}"
                f" + 收下的快照里被抽掉的节 {snap_gone} = {total}）"]
    return []


def check_conserved(rows_before, rows_after):
    """④ append → 重读：行数守恒（新那节必须落稳，一节不许多一节不许少）。"""
    if rows_after != rows_before + 1:
        return [f"④ 行数不守恒：重读 {rows_after} 节（该 {rows_before + 1}）"]
    return []


def check_chain(book, where):
    """①④ 链校验：圈号 1..n 连号、节上的 key 就是这个地址；recent 与独立口径一致。"""
    errs = []
    broke = chain_break_at(book)
    if broke:
        errs.append(f"链断了（{where}）：{broke}")
    if not recent_matches(book, 3):
        errs.append(f"recent 与独立口径对不上（{where}）")
    return errs


# ══════════════ 一个种子的全过程 ══════════════

def run_seed(seed, workdir):
    """攒真账 → 破坏 → 五不变量逐条问。返回可 json 化的结果（含错误清单）。"""
    rng, techs = plan(seed)
    t_all = time.perf_counter()
    work = os.path.join(workdir, f"seed{seed}")
    os.makedirs(work, exist_ok=True)
    orig, other = os.path.join(work, "orig"), os.path.join(work, "other")
    snap_a, jp_a = build(orig, "A", KA)
    snap_b, jp_b = build(other, "B", KB)
    truth = total_rows(Ledger.load(snap_a, writer="fuzz", empty_ok=True))
    snap_truth = snapshot_nodes(snap_a)                # 原账快照里的节数
    site = os.path.join(work, "site")
    shutil.copytree(orig, site)                        # 现场另拷一份，原账留着当尺
    snap = os.path.join(site, "A", "book.json")
    jp = snap + ".jsonl"
    raw_j, raw_s = open(jp, "rb").read(), open(snap, "rb").read()
    other_j = open(jp_b, "rb").read() if "splice" in techs else b""
    other_s = open(snap_b, "rb").read() if "splice" in techs else b""
    new_j = corrupt(raw_j, rng, techs, other_j)        # 打在流水上
    new_s = corrupt(raw_s, rng, techs, other_s)        # 快照上再来一遍
    with open(jp, "wb") as f:
        f.write(new_j)
    with open(snap, "wb") as f:
        f.write(new_s)
    snap_phys = snapshot_nodes(snap)                   # 破坏落盘之后再数：还剩几节
    extra = 6 if "splice" in techs else 0              # 拼接：流水 3 节 + 快照 3 节

    errs = []
    before = (open(snap, "rb").read(), open(jp, "rb").read())
    t0 = time.perf_counter()
    L, err = None, None
    try:
        L = Ledger.load(snap, writer="fuzz", empty_ok=True)
    except BaseException as e:                         # noqa: BLE001 —— 要的就是把异常交出来判
        err = e
    elapsed = time.perf_counter() - t0
    after = (open(snap, "rb").read(), open(jp, "rb").read())
    errs += check_bytes_unchanged(before, after, "load")

    refused = False
    if err is not None:
        errs += check_refusal(err, snap)
        os.replace(snap, snap + ".corrupt")            # 处理：坏本挪开，同目录接着用
        refused = True                                 # 快照整份不在账里了（底会整份重读）

    # ④ 处理之后同目录还能干活（被拒的已经挪开，收下的原样）
    try:
        L2 = Ledger.load(snap, writer="fuzz", empty_ok=True)
    except BaseException as e:                         # noqa: BLE001
        errs.append(f"④ 处理之后 load 不动了：{type(e).__name__}: {e}")
        L2 = None

    rows = total_rows(L) if L is not None else 0
    rows2 = total_rows(L2) if L2 is not None else 0
    bound = truth + extra
    errs += check_rows_bound(rows, bound, "载入")
    errs += check_rows_bound(rows2, bound, "处理后")

    # ③ 诚实读数：坏行数对着独立数出来的那一份
    bad = int(L2.stat.get("bad_lines") or 0) if L2 is not None else 0
    try:
        with open(snap, encoding="utf-8") as f:
            upto = json.load(f).get("_journal_upto", 0)
    except Exception:                                  # noqa: BLE001 —— 快照读不动 = 从头读
        upto = 0
    dbad = definitely_bad_lines(jp, upto if isinstance(upto, int) else 0)
    errs += check_honest(bad, dbad)

    # ③ 少的能说出少哪
    missing = truth - rows2
    if missing > 0:
        absent = sum(1 for ln in raw_j.split(b"\n")
                     if ln.strip() and ln not in set(new_j.split(b"\n")))
        # 被拒的快照：底与快照同源、会整份重读 → 那几节补得回来，记 0；
        # 收下的快照：底按 sealed 位置整份跳过 → 快照里被抽掉的节就真没了，记下来。
        snap_gone = 0 if refused else max(0, snap_truth - snap_phys)
        errs += check_missing(missing, absent, bad, snap_gone)

    # ④ append → 重读：行数守恒、链不断、recent 对得上
    try:
        line = L2.append(jp, addr("fuzz之后"), 1, "坏文件之后那一节", "fp|after",
                         writer="fuzz")
        if line is None:
            errs.append("④ append 被准入闸拦下（口径不该变）")
        L3 = Ledger.load(snap, writer="fuzz", empty_ok=True)
        errs += check_conserved(rows2, total_rows(L3))
        errs += check_chain(L3, "append 之后")
    except BaseException as e:                         # noqa: BLE001
        errs.append(f"④ 处理后同目录用不动：{type(e).__name__}: {e}")

    for name, book in (("载入", L), ("处理后", L2)):
        if book is not None:
            errs += [f"{name}的{e}" for e in check_chain(book, name)]

    total = time.perf_counter() - t_all
    errs += check_time(total, BUDGET_S, f"种子 {seed} 全过程")
    return {"seed": seed, "techs": techs, "truth": truth, "rows": rows, "rows2": rows2,
            "bound": bound, "bad": bad, "dbad": dbad, "refused": refused,
            "snap_phys": snap_phys, "snap_truth": snap_truth,
            "elapsed": total, "errs": errs}


# ══════════════ 子进程那一侧 ══════════════

def _run_in_child(seed, d):
    """父进程起子进程跑一个种子：掐表（挂了/超了当场 kill 判红），只信子进程交回的 JSON。"""
    t0 = time.perf_counter()
    p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--run", str(seed), d],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=REPO)
    deadline = t0 + BUDGET_S
    while p.poll() is None:
        if time.perf_counter() > deadline:
            p.kill()
            p.wait()
            return {"dead": f"② 挂死：种子 {seed} 超过 {BUDGET_S}s 线，已 kill"}
        time.sleep(0.02)
    out, err = p.communicate(timeout=10)
    if p.returncode != 0:
        tail = err.decode("utf-8", "replace").strip().splitlines()[-6:]
        return {"dead": f"② 打死：子进程退出码 {p.returncode}（种子 {seed}）：\n      "
                        + "\n      ".join(tail)}
    try:
        res = json.loads(out.decode("utf-8"))
    except Exception as e:                             # noqa: BLE001
        return {"dead": f"② 子进程没交出结果（{type(e).__name__}: {e}）："
                        f"{out[:300]!r} stderr={err[:300]!r}"}
    res["wall"] = time.perf_counter() - t0
    return res


# ══════════════ 测试 ══════════════

def test_seed_set_and_technique_coverage():
    """任务书硬线：12 个固定种子；五种手法在种子面上各至少出现 2 次（挑种子时就照这个挑的）。"""
    assert len(SEEDS) == 12, f"种子只有 {len(SEEDS)} 个（任务书：12 个）"
    assert len(set(SEEDS)) == len(SEEDS), "种子重了"
    seen = {}
    for s in SEEDS:
        for t in plan(s)[1]:
            seen[t] = seen.get(t, 0) + 1
    assert set(seen) == set(TECHS), f"手法没铺满：{sorted(seen)}"
    thin = {t: n for t, n in seen.items() if n < 2}
    assert not thin, f"有手法只覆盖到 1 次，种子面太窄：{thin}"


def test_journal_line_key_mismatch_is_a_bad_line(tmp_path):
    """根因钉子（round 61 抓到的第 1 处）：流水里节上署的地址与记它的地址对不上 = 坏行。

    随机字节打在 `key` 字段上，这一行照样 parse 得动、照样长得像一节账 ——
    收下就等于把别人的账挂到这个地址下面，链校验（节和地址必须对上）当场断。
    """
    jp = str(tmp_path / "j.jsonl")
    good = {"key": "k", "verdict": 1, "reason": "好行", "ts": 1.0, "ctx_fp": "",
            "hits": 0, "round": 1, "value_score": None, "tombstone": None,
            "last_hit": None, "borrows": []}
    with open(jp, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"t": "node", "key": "k", "node": good},
                           ensure_ascii=False) + "\n")
        # 节上署的是别人的地址（随机字节打在 key 字段上就是这个样子）
        f.write(json.dumps({"t": "node", "key": "k",
                            "node": dict(good, key="别人的地址", reason="坏行")},
                           ensure_ascii=False) + "\n")
        f.write(json.dumps({"t": "node", "key": "k",
                            "node": dict(good, round=None, ts=2.0, reason="后面那行好行")},
                           ensure_ascii=False) + "\n")
    L = Ledger(writer="t", ttl=None)
    L.replay(jp)
    assert L.stat.get("bad_lines") == 1, "地址对不上的行没进 bad_lines（吞了坏行）"
    assert len(L.nodes.get("k") or []) == 2, "一条坏行把前后的账也关在门外"
    assert chain_break_at(L) is None, f"链断了：{chain_break_at(L)}"


@pytest.mark.parametrize("shape", ["地址对不上", "圈号断了"])
def test_snapshot_chain_violation_is_refused(tmp_path, shape):
    """根因钉子（round 61 抓到的第 2 处）：快照里链的两条硬规矩（地址、1..n 连号）必须过尺。

    以前这两条只在用尺的人那边查（`tests/_invariants.chain_break_at`），读这一侧一格不验 ——
    JSON 照样合法、照收，载入出来的账链当场是坏的。
    """
    snap = str(tmp_path / "book.json")
    jp = snap + ".jsonl"
    L = Ledger(writer="t", ttl=None)
    assert L.append(jp, "k", 1, "第一节", "fp|1", writer="t") is not None
    L.save(snap)
    with open(snap, encoding="utf-8") as f:
        d = json.load(f)
    if shape == "地址对不上":
        d["nodes"]["别人的地址"] = d["nodes"].pop("k")
    else:
        d["nodes"]["k"][0]["round"] = 2
    with open(snap, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    broken = open(snap, "rb").read()

    with pytest.raises(ValueError) as ei:
        Ledger.load(snap, writer="t", empty_ok=True)
    msg = str(ei.value)
    assert os.path.basename(snap) in msg, f"拒绝没点名文件：{msg}"
    assert open(snap, "rb").read() == broken, "拒绝却动了原文件一个字节"

    os.replace(snap, snap + ".corrupt")                 # 挪开坏本，同目录照常干活
    L2 = Ledger.load(snap, writer="t", empty_ok=True)
    assert L2.append(jp, "k2", 1, "挪开坏本之后那节", "fp|2", writer="t") is not None
    L3 = Ledger.load(snap, writer="t", empty_ok=True)
    assert total_rows(L3) == 2, f"挪开坏本后该有 2 节，实得 {total_rows(L3)}"
    assert chain_break_at(L3) is None, f"链断了：{chain_break_at(L3)}"


@pytest.mark.parametrize("seed", SEEDS)
def test_file_fuzz_five_invariants(seed, tmp_path):
    """一个种子一条：五不变量全过才算绿（子进程跑，父进程 10s 掐表）。"""
    t0 = time.perf_counter()
    res = _run_in_child(seed, str(tmp_path))
    wall = time.perf_counter() - t0
    errs = []
    if res.get("dead"):
        errs.append(res["dead"])
    else:
        errs.extend(res.get("errs") or [])
        if res.get("elapsed", 0) > BUDGET_S:
            errs.append(f"单种子耗时 {res['elapsed']:.1f}s（> {BUDGET_S}s 线）")
    _RESULTS[seed] = res
    assert not errs, f"【种子 {seed}】五不变量没全过：\n" + "\n".join(errs[:20])
    print(f"\n[fuzz:{seed}] 手法 {','.join(res['techs'])} · "
          f"{'快照被拒（挪开）' if res['refused'] else '快照收下'} · "
          f"原账 {res['truth']} 节 → 处理后 {res['rows2']} 节（上限 {res['bound']}）· "
          f"坏行 {res['bad']} / 独立数 {res['dbad']} · "
          f"子进程 {res['elapsed']:.2f}s / 墙钟 {wall:.2f}s")


def test_coverage_walks_every_path(tmp_path):
    """12 个种子必须把四条路都走到：快照收下 / 被拒 / 流水里有坏行 / 一行坏行都没有。

    防的是「全被拒所以全绿」那种假覆盖 —— 只测得到拒绝，等于没测读进去的那条路。
    """
    rows = []
    for s in SEEDS:
        res = _RESULTS.get(s)
        if res is None or res.get("dead") or res.get("errs"):
            res = run_seed(s, str(tmp_path))            # 单跑这条测试时自己跑一遍
            assert not res["errs"], f"种子 {s}：{res['errs']}"
        rows.append(res)
    acc = sum(1 for r in rows if not r["refused"])
    ref = sum(1 for r in rows if r["refused"])
    bad = sum(1 for r in rows if r["dbad"] > 0)
    clean = sum(1 for r in rows if r["dbad"] == 0)
    assert acc >= 2, f"「快照被收下」只走到 {acc} 个种子（该 ≥2）—— 读进去那条路没测到"
    assert ref >= 4, f"「快照被拒」只走到 {ref} 个种子（该 ≥4）"
    assert bad >= 4, f"「流水里有坏行」只走到 {bad} 个种子（该 ≥4）"
    assert clean >= 2, f"「一行坏行都没有」只走到 {clean} 个种子（该 ≥2）"
    print("\n种子 | 手法 | 快照 | 处理后/原账/上限 | 坏行(账本/独立) | 耗时")
    for r in rows:
        print(f"  {r['seed']:>3} | {','.join(r['techs']):<24} | "
              f"{'拒' if r['refused'] else '收'} | "
              f"{r['rows2']}/{r['truth']}/{r['bound']} | "
              f"{r['bad']}/{r['dbad']} | {r['elapsed']:.3f}s")


def test_guard_the_rulers_tick():
    """护栏自检（round 60 的手法）：八把尺各喂一份**故意打坏**的输入，必须真响；
    再各喂一份干净输入，必须不响 —— 尺子睡着（永远绿）和尺子坏了（永远红）都不许。"""
    p = os.path.join("some", "dir", "book.json")        # 只用文件名，不碰任何真文件
    # ① 拒绝的尺
    assert check_refusal(RuntimeError("boom"), p), "没接住的异常没被点名"
    assert check_refusal(ValueError("快照坏了"), p), "没点名文件的拒绝没被点名"
    assert not check_refusal(ValueError(f"快照读不动（文件坏了）：{p} —— x"), p)
    assert not check_refusal(None, p), "没异常也报错 = 尺子坏了"
    # ② 掐表
    assert check_time(11, 10), "超线没响"
    assert not check_time(1, 10), "没超线也响 = 尺子坏了"
    # ⑤ 字节
    assert check_bytes_unchanged(b"a", b"b"), "动了字节没响"
    assert not check_bytes_unchanged(b"a", b"a"), "没动字节也响 = 尺子坏了"
    # ③ 行数上界
    assert check_rows_bound(7, 6), "行数变多没响"
    assert not check_rows_bound(6, 6), "没变多也响 = 尺子坏了"
    # ③ 假绿
    assert check_honest(0, 2), "坏行数不够没响"
    assert not check_honest(2, 2), "够了也响 = 尺子坏了"
    # ③ 少的说不出少哪
    assert check_missing(3, 1, 0, 0), "少了说不出来没响"
    assert not check_missing(1, 1, 0, 0), "说得出来也响 = 尺子坏了"
    # ④ 行数守恒
    assert check_conserved(5, 7), "行数不守恒没响"
    assert not check_conserved(5, 6), "守恒也响 = 尺子坏了"
    # ①④ 链校验
    broken = Ledger(writer="g", ttl=None)
    broken.nodes["k"] = [{"key": "k", "verdict": 1, "round": 2, "ts": 1.0}]
    assert check_chain(broken, "故意打坏的链"), "断了的链没被点名"
    ok = Ledger(writer="g", ttl=None)
    ok.nodes["k"] = [{"key": "k", "verdict": 1, "round": 1, "ts": 1.0}]
    assert not check_chain(ok, "干净的链"), "干净的链也报错 = 尺子坏了"


def test_total_time_within_120s():
    """任务书硬线：这个文件从头到尾 ≤ 120s。"""
    took = time.perf_counter() - T0
    assert took <= TOTAL_BUDGET_S, f"全文件跑了 {took:.1f}s（> {TOTAL_BUDGET_S}s 线）"
    print(f"\n[file_fuzz] 12 种子 + 覆盖度 + 两颗钉子 + 护栏自检，全文件 {took:.1f}s"
          f"（线 {TOTAL_BUDGET_S}s）")


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--run":
        print(json.dumps(run_seed(int(sys.argv[2]), sys.argv[3]),
                         ensure_ascii=False), end="")
        raise SystemExit(0)
    raise SystemExit("跑法：python -m pytest -q tests/test_file_fuzz.py")

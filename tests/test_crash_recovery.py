# -*- coding: utf-8 -*-
"""崩溃注入恢复测试（round 57）—— 账死在半路，账还完不完好、锁还接不接得回来。

round 56 把「多进程活着」的并发压测补齐了；这一份补的是另一块空白：
**进程死在半路**（崩溃 / 断电式硬杀，`os._exit` 注入钩子）之后，账与锁还站不站得住。

六个注入点（任务书要求 ≥3，这里是 6 个，正好盖住三条写路）：

  ① append·写完没放锁      死在 `_write_line_unlocked` 返回之前（行已落盘、锁还在）
  ② append·写到一半        死在 write 中间：只有半截字节落了盘（半行残尾）
  ③ save·replace 之前      tmp 写完了、`os.replace` 还没换（快照该是旧的那份）
  ④ save·replace 之后      换完了、`_save_locked` 的收尾还没跑（快照该是新的那份）
  ⑤ compact·拷底之后       `shutil.copyfile` 拿到备份、正本还没截空
  ⑥ compact·截空之后       正本已截空、第二次 save 还没进门（备份 + 旧快照兜底）

每个点的流程都是同一条三段：

  段一「杀在半路」  子进程跑到崩点 `os._exit(9)`，母进程读它留下的现场。
  段二「杀完再开」  母进程立刻 `load`：行数守恒、链不断、半行与坏 JSON 不进账、
                    残留锁判得死、收得回。
  段三「别人接着干」另起一个真进程（复活进程）在同一本账上 append + save + compact，
                    必须在预算内跑完 —— 死人的锁不许把活人挡住（不许死锁）。
                    跑完再开一次账：一行半行都没有、坏 JSON 为 0、行数仍守恒、
                    锁一个不剩。

「行数守恒」的口径（任务书原话：崩在写前 = 没写不算丢；崩在写中 = 那行允许缺
但不许半行）：崩点之前的节数**一节都不许少**，崩点那一节 landed=True 就必须在、
landed=False 就允许缺（但它的残尾不许拖累后面任何一行）。

全程定值、没有随机数（换台机器同一条序列，`SEED` 与 round 56 同号留作锚）、
pytest 临时目录里跑，真实账一个字节不碰；单点 ≤30 秒（`BUDGET_S`）。
"""
import gc
import json
import os
import subprocess
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ledger import Ledger, addr                # noqa: E402
from _invariants import chain_break_at, recent_matches, total_rows  # noqa: E402

SEED = 20261107              # 固定：换台机器同一条序列（与 round 56 同号）
CRASH_AT = 5                 # append 点：第 5 次写触发崩溃（前 4 次正常落账）
BATCH = 3                    # save 点：崩之前先正常存过盘的节数（每批）
COMPACT_ROWS = 4             # compact 点：压实之前落的节数
WORKER_ROWS = 4              # 复活进程要落的节数
BUDGET_S = 30                # 任务书硬线：每轮 ≤ 30 秒
CHILD_TIMEOUT = 25           # 子进程跑不完 = 卡死，当红处理
WORKER_TIMEOUT = 25          # 复活进程跑不完 = 死锁，当红处理

POINTS = ["append_after_write", "append_mid_write",
          "save_before_replace", "save_after_replace",
          "compact_after_copy", "compact_after_truncate"]

CRASH_MARK = "@@CRASH@@"
WORKER_MARK = "@@WORKER@@"


# ════════════════ 子进程：跑到崩点硬死 ════════════════

def _die(point, payload):
    """把现场交出去然后硬死（os._exit 不跑 finally —— 正是「死在半路」要的形状）。"""
    sys.stdout.write(CRASH_MARK + " " + json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    os._exit(9)


def child_main(point, d):
    snap = os.path.join(d, "book.json")
    jp = snap + ".jsonl"
    L = Ledger(writer="victim", ttl=None)
    committed = []

    def add(i):
        line = L.append(jp, addr("崩溃现场"), 1 if i % 2 else 0,
                        reason=f"第{i}节", ctx_fp=f"fp|{i % 3}", writer="victim")
        if line is None:
            _die(point, {"error": f"第{i}节被准入闸拦下（前提不成立）"})
        committed.append(line["node"]["nid"])

    if point.startswith("append_"):
        orig = Ledger._write_line_unlocked
        st = {"n": 0}

        def hook(path, data, blob):
            st["n"] += 1
            if st["n"] < CRASH_AT:
                return orig(path, data, blob)
            nid = json.loads(data).get("node", {}).get("nid")
            if point == "append_after_write":
                orig(path, data, blob)              # 行写完了，锁还没放
                _die(point, {"committed": committed, "attempted": nid, "landed": True})
            # append_mid_write：死在 write 中间 —— 只有半截字节落到盘上
            half = blob[: max(1, len(blob) // 2)]
            fd = os.open(path, os.O_WRONLY | os.O_APPEND)
            try:
                os.write(fd, half)
            finally:
                os.close(fd)
            _die(point, {"committed": committed, "attempted": nid, "landed": False})

        Ledger._write_line_unlocked = hook
        for i in range(1, CRASH_AT + 1):
            add(i)
        _die(point, {"error": "崩点没触发（append 没走到第 5 次写）"})

    elif point.startswith("save_"):
        for i in range(1, BATCH + 1):
            add(i)
        L.save(snap)                                # 旧本先立起来（崩之前真存过一次）
        for i in range(BATCH + 1, BATCH * 2 + 1):
            add(i)
        orig_replace = os.replace

        def boom(src, dst):
            hit = (os.path.abspath(str(dst)) == os.path.abspath(snap)
                   and str(src).endswith(".tmp"))
            if hit and point == "save_before_replace":
                _die(point, {"committed": committed, "landed": False})
            r = orig_replace(src, dst)
            if hit and point == "save_after_replace":
                _die(point, {"committed": committed, "landed": True})
            return r

        os.replace = boom
        L.save(snap)
        _die(point, {"error": "崩点没触发（save 没走到 replace）"})

    elif point.startswith("compact_"):
        for i in range(1, COMPACT_ROWS + 1):
            add(i)
        L.save(snap)
        if point == "compact_after_copy":
            import shutil
            orig_copy = shutil.copyfile

            def copy_boom(src, dst):
                orig_copy(src, dst)                 # 备份拿到了，正本还没截空
                _die(point, {"committed": committed, "landed": True})

            shutil.copyfile = copy_boom
        else:                                       # compact_after_truncate
            orig_save = Ledger.save
            st = {"n": 0}

            def save_boom(self, path, compact_journal=False, journal=None):
                st["n"] += 1
                if st["n"] >= 2:                    # 第二次 save 进门 = 正本已截空
                    _die(point, {"committed": committed, "landed": True})
                return orig_save(self, path, compact_journal=compact_journal,
                                 journal=journal)

            Ledger.save = save_boom
        L.compact(snap, journal=jp)
        _die(point, {"error": "崩点没触发（compact 没走到）"})

    _die(point, {"error": f"不认识的崩点 {point}"})


# ════════════════ 子进程：复活进程（别人接着干） ════════════════

def worker_main(d):
    snap = os.path.join(d, "book.json")
    jp = snap + ".jsonl"
    out = {"appended": [], "errors": [], "compact": None}
    try:
        L = Ledger(writer="rescue", ttl=None)
        for i in range(WORKER_ROWS):
            line = L.append(jp, addr("复活现场"), 1 if i % 2 else 0,
                            reason=f"复活第{i}节", ctx_fp=f"fp|r{i}", writer="rescue")
            if line is None:
                out["errors"].append(f"复活第{i}节被准入闸拦下")
            else:
                out["appended"].append(line["node"]["nid"])
        L.save(snap)
        ret = L.compact(snap, journal=jp)
        out["compact"] = "skipped" if ret is False else ("none" if ret is None else "ok")
        if ret is False:
            out["errors"].append(f"压实让路了（死人的锁还没回收？stat={L.stat}）")
    except BaseException as e:                       # noqa: BLE001 —— 复活进程崩了也算红
        out["errors"].append(f"{type(e).__name__}: {e}")
    sys.stdout.write(WORKER_MARK + " " + json.dumps(out, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0 if not out["errors"] else 1


# ════════════════ 母进程的尺 ════════════════

def _run_proc(mode, point, d, timeout):
    """跑一个子进程（--child 崩点 / --worker 复活），返回 (返回码, 载荷, 耗时, stderr)。"""
    args = [sys.executable, os.path.abspath(__file__), mode]
    if mode == "--child":
        args += [point]
    args += [d]
    t0 = time.perf_counter()
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=REPO)
    except subprocess.TimeoutExpired as e:
        return None, None, time.perf_counter() - t0, (
            f"子进程 {mode} {point} 超过 {timeout}s 没退出（疑似卡死/死锁）"
            f"：stdout={e.stdout!r}")
    mark = CRASH_MARK if mode == "--child" else WORKER_MARK
    payload = None
    for ln in (p.stdout or "").splitlines():
        if ln.startswith(mark):
            payload = json.loads(ln[len(mark):].strip())
    return p.returncode, payload, time.perf_counter() - t0, (p.stderr or "").strip()


def _journal_state(jp):
    """流水现场：(完整行数, 坏行清单, 是否以换行收尾, 残尾字节数)。

    只数**以换行收尾的行**（读的人 `_absorb` 也只在换行处切）；没换行的残尾
    单独报，不混进坏行 —— 残尾是崩在写中允许留的，坏行是绝不许进账的。
    """
    if not os.path.exists(jp):
        return 0, [], True, 0
    with open(jp, "rb") as f:
        raw = f.read()
    ends_nl = (not raw) or raw.endswith(b"\n")
    parts = raw.split(b"\n")
    tail = b"" if ends_nl else parts.pop()
    torn = len(tail)
    bad, n = [], 0
    for i, ln in enumerate(parts, 1):
        if not ln.strip():
            continue
        n += 1
        try:
            json.loads(ln.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            bad.append(f"第{i}行 parse 不动（{type(e).__name__}: {e}）：{ln[:60]!r}")
    return n, bad, ends_nl, torn


def _snapshot_ok(snap):
    """快照读得动就给 None，读不动给一句话；不存在也给 None（崩在存盘前很常见）。"""
    if not os.path.exists(snap):
        return None
    try:
        with open(snap, encoding="utf-8") as f:
            json.load(f)
    except Exception as e:                          # noqa: BLE001
        return f"快照读不动：{type(e).__name__}: {e}"
    return None


def _nids(L):
    return {n.get("nid") for c in L.nodes.values() for n in c}


def _check_book(L, expected, where):
    """崩完开出来的账要过的四条：链不断、行数守恒、坏行不进账、recent 自洽。"""
    errs = []
    broke = chain_break_at(L)
    if broke:
        errs.append(f"[{where}] ① 链断了：{broke}")
    got = _nids(L)
    if got != set(expected):
        errs.append(f"[{where}] ② 行数不守恒：链上 {len(got)} 节 != 该有的 {len(expected)} 节"
                    f"；少了 {sorted(set(expected) - got)[:3]}；多了 {sorted(got - set(expected))[:3]}")
    if total_rows(L) != len(expected):
        errs.append(f"[{where}] ② 链上 {total_rows(L)} 节 != 落账 {len(expected)} 节")
    bad = int(L.stat.get("bad_lines") or 0)
    if bad:
        errs.append(f"[{where}] ③ 坏 JSON 进了账：bad_lines={bad}")
    for n in (0, 1, 7, 40):
        if not recent_matches(L, n):
            errs.append(f"[{where}] ⑤ recent({n}) 与独立口径不一致")
            break
    return errs


def _residue_locks(d):
    return sorted(n for n in os.listdir(d) if n.endswith(".lock"))


def _lock_pid(path):
    try:
        raw = open(path, encoding="utf-8", errors="ignore").read().strip()
        return int(raw.split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _wait_lock_owner_dead(path, timeout=6.0):
    """等残留锁的主人**真的**死透，再决定走哪条判死路。

    Windows 的坑（round 57 实测）：子进程刚 `os._exit` 的那一瞬，它的进程对象还有人
    握着句柄 —— `OpenProcess` 仍成功、`GetExitCodeProcess` 拿得到 9（死人），这一刻
    **谁都判它活着**（实测约 1 秒内，连第三方进程也一样）。所以不能崩完立刻判，
    那会把死锁误判成活锁、白等 45 秒；等它真没了（err=87）才是判死的钟点。
    等不到 = pid 被系统复用给活人了 —— 那才轮到 stale_after 兜底（真系统里等 45 秒，
    这里把钟拨过期，同一个判据、只是不真等）。

    返回 (一句话说明, 是不是走了 stale_after 兜底)。
    """
    pid = _lock_pid(path)
    if pid is None:
        return "垃圾锁（读不出 pid）", False
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not Ledger._alive(pid):
            return f"pid {pid} 已死（当场接管）", False
        time.sleep(0.1)
    old = time.time() - 60                      # 超过 stale_after=45 的兜底线
    try:
        os.utime(path, (old, old))
    except OSError:
        pass
    return f"pid {pid} 被复用给活人 → 按 stale_after 过期判死", True


# ════════════════ 测试 ════════════════

@pytest.mark.parametrize("point", POINTS)
def test_crash_injection_recovers(point, tmp_path):
    """六个崩点逐个打：杀在半路 → 杀完再开 → 别人接着干，三段全绿才算过。"""
    t0 = time.perf_counter()
    d = str(tmp_path)
    snap = os.path.join(d, "book.json")
    jp = snap + ".jsonl"
    errs = []

    # ── 段一：杀在半路 ──
    rc, payload, took_c, err = _run_proc("--child", point, d, CHILD_TIMEOUT)
    if rc is None:
        errs.append(err)
        assert not errs, "\n".join(errs)
    if rc != 9:
        errs.append(f"子进程没死在崩点上（returncode={rc}），payload={payload} stderr={err[:400]}")
    if not payload or payload.get("error"):
        errs.append(f"崩点没交出现场：payload={payload}")
    committed = list((payload or {}).get("committed") or [])
    landed = bool((payload or {}).get("landed"))
    attempted = (payload or {}).get("attempted")
    # 崩点那一节：写完了（landed）就必须在；没写完就允许缺（但残尾不许拖累别人）
    expected = list(committed) + ([attempted] if (landed and attempted) else [])
    assert not errs, ("\n".join(errs))

    # ── 段二：杀完再开 ──
    L1 = Ledger.load(snap, journal=jp, writer="chk", empty_ok=True)
    errs += _check_book(L1, expected, "崩完就开")
    snap_err1 = _snapshot_ok(snap)
    if snap_err1:
        errs.append("[崩完就开] " + snap_err1)
    n1, bad1, ends_nl1, torn1 = _journal_state(jp)
    if bad1:
        errs.append("[崩完就开] ③ 流水里有 parse 不动的行：" + "；".join(bad1[:3]))
    if not ends_nl1 and point != "append_mid_write":
        errs.append(f"[崩完就开] ③ 流水尾巴没有换行（半行/被截断），残尾 {torn1} 字节")
    if point == "append_mid_write":
        # 崩在写中允许留残尾（那行允许缺），但残尾必须是**惰性的**：
        # 一截 parse 不动的 JSON 前缀，不许被当成一行进账。
        if not torn1:
            errs.append("[崩完就开] ③ append_mid_write 没留下残尾：崩点没打中 write 中间")
        else:
            tail = open(jp, "rb").read().split(b"\n")[-1]
            try:
                json.loads(tail.decode("utf-8"))
                errs.append("[崩完就开] ③ 残尾 parse 得动（它是一整行，崩点没打中 write 中间）")
            except (ValueError, UnicodeDecodeError):
                pass                          # 正是半行：读的人不碰它
    # 残留锁：崩溃点必须真留下锁（没留下 = 崩点没打中锁内），且判得死、收得回
    residue = _residue_locks(d)
    if not residue:
        errs.append(f"[崩完就开] ④ {point} 没留下任何残留锁：崩点没打中锁内")
    lock_note = []
    seen = {}                                   # 同一个 pid 只等一次（压实点有两把锁同主）
    for n in residue:
        path = os.path.join(d, n)
        pid = _lock_pid(path)
        if pid in seen:
            lock_note.append(seen[pid])
            continue
        note, aged = _wait_lock_owner_dead(path)
        if aged:
            note += "（测试把钟拨过期，不真等 45 秒）"
        seen[pid] = note
        lock_note.append(note)

    # ── 段三：别人接着干（真进程，锁必须收得回、不许死锁） ──
    rc2, wpayload, took_w, err2 = _run_proc("--worker", "", d, WORKER_TIMEOUT)
    if rc2 is None:
        errs.append(err2)
    elif rc2 != 0:
        errs.append(f"复活进程没跑完（returncode={rc2}）：payload={wpayload} stderr={err2[:400]}")
    worker_rows = list((wpayload or {}).get("appended") or [])
    if len(worker_rows) != WORKER_ROWS:
        errs.append(f"复活进程只落了 {len(worker_rows)}/{WORKER_ROWS} 节：{wpayload}")

    # ── 干完再开：一行半行都不许剩、坏 JSON 为 0、行数仍守恒、锁一个不剩 ──
    L2 = Ledger.load(snap, journal=jp, writer="chk", empty_ok=True)
    errs += _check_book(L2, expected + worker_rows, "干完再开")
    n2, bad2, ends_nl2, torn2 = _journal_state(jp)
    if bad2:
        errs.append("③ 干完再开：流水里有 parse 不动的行（半行拖累了后面的行）："
                    + "；".join(bad2[:3]))
    if not ends_nl2:
        errs.append(f"③ 干完再开：流水尾巴还有 {torn2} 字节残尾（半行没被清掉）")
    snap_err = _snapshot_ok(snap)
    if snap_err:
        errs.append("③ 干完再开：" + snap_err)
    locks = _residue_locks(d)
    if locks:
        errs.append(f"④ 干完再开：锁残留 {locks}（复活进程该把死人的锁收掉）")
    # 快照必须是复活进程存过的那份（崩在 replace 之前的 tmp 不许顶上来）
    if snap_err is None and os.path.exists(snap) and worker_rows:
        if not _nids(L2) >= set(worker_rows):
            errs.append("③ 干完再开：复活进程的节没进快照")

    took = time.perf_counter() - t0
    assert not errs, (f"【{point}】崩点恢复失败（{took:.1f}s）：\n" + "\n".join(errs[:20]))
    print(f"\n[crash:{point}] 崩 {took_c:.1f}s / 复活 {took_w:.1f}s · "
          f"崩点前 {len(committed)} 节、崩点那节 landed={landed} · 残留锁 {residue}（{lock_note}）· "
          f"流水 {n1}→{n2} 行、坏行 0 · 锁残留 0 · 共 {took:.1f}s")
    assert took <= BUDGET_S, f"这一轮 {took:.1f}s（> {BUDGET_S}s 线）"


def test_stale_lock_is_reaped_by_liveness_and_stale_after(tmp_path):
    """残留锁两条判死路都要真：主人已死当场接管；主人活着但 mtime 过了 stale_after 也接管。

    崩点测试里留下的锁走的是第一条（死 pid）；这一条把第二条（stale_after 兜底）
    单独钉住 —— pid 会被系统复用给活人（round 56 注释：150 个短命进程 23% 撞号），
    光看 pid 会把死锁看成活锁，最后还得靠 mtime 兜底。
    """
    lock = str(tmp_path / "book.json.jsonl.lock")

    # ① 主人活着、锁还新鲜：一个字节都不许碰（互斥优先）
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        with open(lock, "w", encoding="utf-8") as f:
            f.write(str(live.pid))
        assert Ledger._lock_own(lock) is None, "新鲜的活锁被抢了（互斥破了）"

        # ② 同一把锁，mtime 过了 stale_after=45：按「没人续命」判死，回收
        old = time.time() - 60
        os.utime(lock, (old, old))
        fd = Ledger._lock_own(lock)
        assert fd is not None, "stale_after 过期的锁没被判死回收"
        with open(lock, encoding="utf-8") as f:
            assert f.read().strip() == str(os.getpid()), "接管后锁里不是我的 pid"
        os.close(fd)
        assert Ledger._drop_lock(lock), "回收完没删掉锁"
    finally:
        live.kill()
        live.wait(timeout=10)

    # ③ 主人已死（真退出的进程）：不等 mtime，当场接管。
    #    刚 wait() 完进程对象还有人握句柄（见 _wait_lock_owner_dead 的说明），先放手再等它死透。
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=10)
    pid = dead.pid
    del dead                                 # 句柄在 Popen 对象上：不放手，进程对象就不消失
    gc.collect()
    t0 = time.time()
    while Ledger._alive(pid) and time.time() - t0 < 6:
        time.sleep(0.1)
    if Ledger._alive(pid):
        pytest.skip(f"pid {pid} 一直被判活着（被复用给活人），测不到死锁这条路")
    with open(lock, "w", encoding="utf-8") as f:
        f.write(str(pid))
    fd = Ledger._lock_own(lock)
    assert fd is not None, "死主人的残留锁没被当场接管"
    os.close(fd)
    Ledger._drop_lock(lock)


def test_torn_tail_never_poisons_the_next_line(tmp_path):
    """半行残尾的钉子（崩在写中那一路）：残尾不许把**后面写进来的行**一起拖成坏行。

    现场：上一个写者死在 write 中间，流水尾巴留一截没换行的 JSON 前缀；
    读的人只在换行处切（`_absorb` 的 rfind），所以残尾本身惰性、不进账 —— 但
    下一个写者是 `open(..., "a")` 追加，整行会**粘在残尾后面**，切出来的是
    「半行 + 新行」的杂交，`json.loads` 当场失败：坏一行，丢一行（那节账从此
    世界上发生过、账上没发生）。这条不 spawn 进程，稳。
    """
    p = str(tmp_path / "book.json")
    jp = p + ".jsonl"
    k = addr("半行现场")

    # 先落两节，再人为造一条残尾（等价于崩在 write 中间）
    a = Ledger(writer="a", ttl=None)
    a.append(jp, k, 1, "第一节", "fp|1", writer="a")
    a.append(jp, k, 0, "第二节", "fp|2", writer="a")
    with open(jp, "ab") as f:
        f.write(b'{"t": "node", "key": "dead', )          # 半截，没有换行

    # 别人接着干：这一行必须整整齐齐落进账，不许跟残尾粘成一行
    b = Ledger(writer="b", ttl=None)
    line = b.append(jp, k, 1, "第三节（残尾之后）", "fp|3", writer="b")
    assert line is not None, "残尾把后面那一节挡掉了"
    c = Ledger.load(p, journal=jp, writer="chk", empty_ok=True)
    assert int(c.stat.get("bad_lines") or 0) == 0, \
        f"残尾拖累了后面的行，坏行进了账：bad_lines={c.stat.get('bad_lines')}"
    nids = _nids(c)
    assert line["node"]["nid"] in nids, "残尾之后写的那一节没进账（被粘没了）"
    assert chain_break_at(c) is None, f"链断了：{chain_break_at(c)}"
    _, bad, ends_nl, _ = _journal_state(jp)
    assert not bad and ends_nl, f"干完流水还有坏行/残尾：bad={bad} ends_nl={ends_nl}"


if __name__ == "__main__":
    # 独立跑：python tests/test_crash_recovery.py --child <崩点> <目录>
    #                            / --worker <目录>
    if len(sys.argv) >= 2 and sys.argv[1] == "--child":
        child_main(sys.argv[2], sys.argv[3])
    elif len(sys.argv) >= 2 and sys.argv[1] == "--worker":
        raise SystemExit(worker_main(sys.argv[2]))
    raise SystemExit("用法：--child <崩点> <目录> | --worker <目录>")

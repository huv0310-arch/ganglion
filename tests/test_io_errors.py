# -*- coding: utf-8 -*-
"""IO 错误注入测试（round 58）—— 磁盘满 / 写失败时：报错诚实吗、账烂不烂、恢复得了吗。

round 56 验的是「多进程都活着」，round 57 验的是「死在半路」；这一份补第三块空白：
**系统调用如实失败**（不是崩溃，是 ENOSPC 盘满 / EACCES 建不了锁这类 IO 错误）打在
四条写路上，每个点问三句（任务书原话）：

  ① 报错诚实吗 —— 抛的是真实错误、**带得出真原因**（errno 对得上、说得出是哪一步），
                  绝不静默假绿、绝不把故障说成「有人在压 / 让路」、绝不行数造假；
  ② 账烂不烂   —— 错误之后**重读正本**：链校验过（`chain_break_at is None`）、
                  流水没有 parse 不动的行、行数守恒（一节不少也一节不多 —— 失败那一节
                  不许凭空出现在盘上）、快照读得动、不留半份垃圾；
  ③ 恢复得了吗 —— 故障源一解除，**同一本账**继续 append → save → compact 全正常，
                  锁一个不剩、`.tmp` 不剩、行数仍守恒。

四个注入点（monkeypatch 打在真调用上，不给被测代码换接口）：

  ① `append_enospc`        写流水那一下 `open(jp, "a")` → ENOSPC
  ② `save_replace_enospc`  `save()` 写 tmp（`os.replace` 之前）→ ENOSPC
  ③ `compact_copy_enospc`  `compact()` 拷底 `shutil.copyfile` → ENOSPC（半份底留在盘上再抛）
  ④ `lock_create_eacces`   建锁 `O_CREAT|O_EXCL` → EACCES（锁文件根本建不出来）

注入点 ④ 同时打两条路：写者的 `<流水>.lock` 与压实的 `<快照>.compact.lock` ——
同一个根因（建不出来 ≠ 不归你），两个调用方都得给出真原因。

固定种子 `SEED`（与 round 56/57 同号留锚），全程 pytest 临时目录，真实账一个字节不碰；
单点 ≤ 30 秒（`BUDGET_S`）。等锁那 60 秒上限在测试里把 `time.sleep` 摸成空操作
（与 `tests/test_known_gaps.py` 同一手法）：钉的是**行为**（抛、带真原因、行没写进去），
不是那 60 秒本身 —— 没人该为一条测试等一分钟。
"""
import builtins
import errno
import json
import os
import random
import shutil
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ledger import Ledger, addr                # noqa: E402
from _invariants import chain_break_at, recent_matches, total_rows  # noqa: E402

SEED = 20261107              # 固定：换台机器同一条序列（与 round 56/57 同号留锚）
PRE_ROWS = 4                 # 注入之前先落的节：错误之后一节都不许少、一节也不许多
RECOVER_ROWS = 2             # 故障解除之后接着落的节
BUDGET_S = 30                # 任务书硬线：每轮 ≤ 30 秒
POINTS = ["append_enospc", "save_replace_enospc",
          "compact_copy_enospc", "lock_create_eacces"]

K = addr("IO 错误注入现场")
K2 = addr("IO 错误注入·恢复现场")
ENOSPC = errno.ENOSPC
EACCES = errno.EACCES


# ════════════════ 注入的引信 ════════════════

def _boom_open(monkeypatch, pred, eno):
    """把 `builtins.open` 换成一根只在 `pred(绝对路径, 模式)` 命中时抛**真错误**的引信。

    命中即 `raise OSError(eno, strerror, filename)` —— errno 与真实盘满/没权限一模一样，
    调用方看到的不是我们编的异常类型，就是系统会给的那种。
    """
    real_open = builtins.open

    def fuse(file, mode="r", *args, **kwargs):
        try:
            p = os.path.abspath(os.fspath(file))
        except TypeError:
            p = None
        if p is not None and pred(p, str(mode)):
            raise OSError(eno, os.strerror(eno), file)
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fuse)


def _boom_copyfile(monkeypatch, jp):
    """拷底那一下 ENOSPC：真盘满是**写到一半**没空间 —— 半份底留在盘上，然后如实抛。"""
    real_copy = shutil.copyfile

    def fuse(src, dst):
        if str(dst).endswith(".compact") and \
                os.path.abspath(str(src)) == os.path.abspath(jp):
            with open(dst, "wb") as f:
                f.write(b'{"t": "node", "key": "half')   # 半份底，没有换行
            raise OSError(ENOSPC, os.strerror(ENOSPC), str(dst))
        return real_copy(src, dst)

    monkeypatch.setattr(shutil, "copyfile", fuse)


def _boom_lock_create(monkeypatch):
    """建锁 `O_CREAT|O_EXCL` → EACCES：锁文件**根本建不出来**（目录只读 / 别人不给写）。

    同时把 `time.sleep` 摸成空操作：等锁上限是 3000 × 0.02s = 60 秒，测试不真等
    （同 `tests/test_known_gaps.py` 的手法，钉行为不钉秒数）。
    """
    real_open = os.open

    def fuse(path, flags, *args, **kwargs):
        if str(path).endswith(".lock") and (flags & os.O_CREAT):
            raise PermissionError(EACCES, os.strerror(EACCES), path)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", fuse)
    monkeypatch.setattr(time, "sleep", lambda _s: None)


# ════════════════ 现场的尺 ════════════════

def _call(fn):
    """跑一个操作，把返回值与异常都交出来（三问要逐条攒，不能撞一次就断）。"""
    try:
        return fn(), None
    except BaseException as e:                     # noqa: BLE001 —— 要的就是把异常交出来判
        return None, e


def _honest(exc, want_errno, what, ret=None):
    """① 报错诚实吗：真抛了、errno 对得上真原因（空表 = 过），并给一句现场说明。"""
    errs = []
    if exc is None:
        errs.append(f"① {what} 没抛错（返回 {ret!r}）—— 故障被吞了，"
                    f"调用方看不出出了什么事")
        return errs, f"没抛错（返回 {ret!r}）"
    if not isinstance(exc, OSError):
        errs.append(f"① {what} 抛的不是 OSError：{type(exc).__name__}: {exc}")
    elif getattr(exc, "errno", None) != want_errno:
        errs.append(f"① {what} 带不出真原因：errno={getattr(exc, 'errno', None)!r}"
                    f"（该是 {want_errno} = {os.strerror(want_errno)}），说辞：{exc}")
    return errs, f"{type(exc).__name__}[errno={getattr(exc, 'errno', None)}] {exc}"


def _nids(L):
    return {n.get("nid") for c in L.nodes.values() for n in c}


def _journal_state(jp):
    """流水现场：(完整行数, 坏行清单, 是否以换行收尾, 残尾字节数) —— 口径同 round 57。

    只数**以换行收尾的行**（读的人 `_absorb` 也只在换行处切）；没换行的残尾单独报，
    不混进坏行。
    """
    if not os.path.exists(jp):
        return 0, [], True, 0
    with open(jp, "rb") as f:
        raw = f.read()
    ends_nl = (not raw) or raw.endswith(b"\n")
    parts = raw.split(b"\n")
    tail = b"" if ends_nl else parts.pop()
    bad, n = [], 0
    for i, ln in enumerate(parts, 1):
        if not ln.strip():
            continue
        n += 1
        try:
            json.loads(ln.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            bad.append(f"第{i}行 parse 不动（{type(e).__name__}: {e}）：{ln[:60]!r}")
    return n, bad, ends_nl, len(tail)


def _residue(d, suffix=(".lock", ".tmp")):
    return sorted(n for n in os.listdir(d) if n.endswith(suffix))


def _check_disk(snap, jp, expected, where):
    """② 账烂不烂：错误之后**重读正本**，五条一起查（空表 = 过）。

    行数守恒用 nid 集合与链长**双向**卡：失败的那一节不许凭空出现在盘上（行数造假），
    成功落账的那一节也不许少（丢账）。
    """
    errs = []
    try:
        Ld = Ledger.load(snap, journal=jp, writer="chk", empty_ok=True)
    except Exception as e:                          # noqa: BLE001 —— 开不出来本身就是红
        return [f"[{where}] ② 账开不出来：{type(e).__name__}: {e}"]
    broke = chain_break_at(Ld)
    if broke:
        errs.append(f"[{where}] ② 链断了：{broke}")
    got = _nids(Ld)
    if got != set(expected) or total_rows(Ld) != len(expected):
        errs.append(f"[{where}] ② 行数不守恒：链上 {total_rows(Ld)} 节、"
                    f"nid {len(got)} 个，该有 {len(expected)} 节；"
                    f"多了 {sorted(got - set(expected))[:3]}、少了 {sorted(set(expected) - got)[:3]}")
    bad = int(Ld.stat.get("bad_lines") or 0)
    if bad:
        errs.append(f"[{where}] ② 坏行进了账：bad_lines={bad}")
    _n, badlines, ends_nl, torn = _journal_state(jp)
    if badlines:
        errs.append(f"[{where}] ② 流水里有 parse 不动的行：" + "；".join(badlines[:2]))
    if not ends_nl:
        errs.append(f"[{where}] ② 流水尾巴没换行（残尾 {torn} 字节 = 半行）")
    if not os.path.exists(snap):
        errs.append(f"[{where}] ② 快照不在")
    else:
        try:
            with open(snap, encoding="utf-8") as f:
                json.load(f)
        except Exception as e:                      # noqa: BLE001
            errs.append(f"[{where}] ② 快照读不动：{type(e).__name__}: {e}")
    for m in (0, 1, 7, 40):
        if not recent_matches(Ld, m):
            errs.append(f"[{where}] ② recent({m}) 与独立口径不一致")
            break
    return errs


def _recover(snap, jp, expected, where, compact=True):
    """③ 恢复得了吗：故障源解除后，**同一本账**（同一套文件）接着 append → save → compact。

    用一个新对象接着干 —— 正本是流水，账的好坏只看盘；失败那个对象内存里还长着一节
    （`append` 的 docstring 写明「副作用照记」），它不 save 就永远落不下去，不掺进这里的账。
    返回 (错误清单, 新落的 nid 列表)。
    """
    errs = []
    d = os.path.dirname(snap)
    L2 = Ledger(writer="rescue", ttl=None)
    added = []
    ret, exc = _call(lambda: [L2.append(jp, K2, 1 if i % 2 else 0,
                                        reason=f"恢复第{i + 1}节", ctx_fp=f"fp|r{i}",
                                        writer="rescue") for i in range(RECOVER_ROWS)])
    if exc is not None:
        errs.append(f"[{where}] ③ 故障解除后还是 append 不动：{type(exc).__name__}: {exc}")
        return errs, added
    for line in ret:
        if line is None:
            errs.append(f"[{where}] ③ 恢复落账被准入闸拦下（口径不该变）")
        else:
            added.append(line["node"]["nid"])
    if compact:
        ret_c, exc_c = _call(lambda: L2.compact(snap, journal=jp))
        if exc_c is not None:
            errs.append(f"[{where}] ③ 故障解除后还是压实不动：{type(exc_c).__name__}: {exc_c}")
        elif not (isinstance(ret_c, str) and ret_c.endswith(".compact")):
            errs.append(f"[{where}] ③ 恢复压实没压成（返回 {ret_c!r}；"
                        f"<备份路径> 才是压成了）")
    else:
        ret_s, exc_s = _call(lambda: L2.save(snap))
        if exc_s is not None:
            errs.append(f"[{where}] ③ 故障解除后还是存不上：{type(exc_s).__name__}: {exc_s}")
    errs += _check_disk(snap, jp, expected + added, where)
    left = _residue(d)
    if left:
        errs.append(f"[{where}] ③ 恢复完还有残留 {left}（锁 / tmp 一个都不许剩）")
    return errs, added


# ════════════════ 测试 ════════════════

@pytest.mark.parametrize("point", POINTS)
def test_io_error_injection_three_questions(point, tmp_path, monkeypatch):
    """四个注入点逐个打：① 报错诚实 ② 账不烂 ③ 恢复得了吗，三问全绿才算过。"""
    t0 = time.perf_counter()
    d = str(tmp_path)
    snap = os.path.join(d, "book.json")
    jp = snap + ".jsonl"
    rng = random.Random(SEED)                       # 固定种子：换台机器同一条序列

    L = Ledger(writer="victim", ttl=None)
    expected = []
    for i in range(PRE_ROWS):
        line = L.append(jp, K, 1 if rng.randrange(2) else 0,
                        reason=f"第{i + 1}节", ctx_fp=f"fp|{i % 3}", writer="victim")
        assert line is not None, "前置落账被准入闸拦下了（口径不该变）"
        expected.append(line["node"]["nid"])
    L.save(snap)                                    # 快照先立起来：注入前的正本在盘上

    q1, q2, q3, notes = [], [], [], {}

    if point == "append_enospc":
        # ── ① 写流水那一下盘满 ──
        before = open(jp, "rb").read()
        _boom_open(monkeypatch, lambda p, m: p == os.path.abspath(jp) and "a" in m, ENOSPC)
        ret, exc = _call(lambda: L.append(jp, K2, 1, "注入那一节", "fp|io", writer="victim"))
        errs, notes["q1"] = _honest(exc, ENOSPC, "append 写流水", ret)
        q1 += errs
        if open(jp, "rb").read() != before:
            q1.append("① 写失败却动了流水一个字节（没写成就该一个字节不动）")
        # ── ② 失败那一节不许凭空出现在盘上（行数造假）──
        q2 += _check_disk(snap, jp, expected, point)
        # ── ③ 故障解除 ──
        monkeypatch.undo()
        errs3, _ = _recover(snap, jp, expected, point)
        q3 += errs3

    elif point == "save_replace_enospc":
        # ── ① save 写 tmp（replace 之前）盘满 ──
        snap_before = open(snap, "rb").read()
        _boom_open(monkeypatch, lambda p, m: p.endswith(".tmp") and "w" in m, ENOSPC)
        ret, exc = _call(lambda: L.save(snap))
        errs, notes["q1"] = _honest(exc, ENOSPC, "save 写 tmp（replace 之前）", ret)
        q1 += errs
        if open(snap, "rb").read() != snap_before:
            q1.append("① 存盘失败却动了旧快照（原子写：replace 之前旧本必须原样）")
        # ── ② 旧快照 + 流水都得原样 ──
        q2 += _check_disk(snap, jp, expected, point)
        left = _residue(d)
        if left:
            q2.append(f"② 存盘失败留了垃圾 {left}（tmp 该由 finally 收走）")
        # ── ③ 故障解除 ──
        monkeypatch.undo()
        errs3, _ = _recover(snap, jp, expected, point, compact=False)
        q3 += errs3
        # 存上了要真进快照（存盘失败那一下不许把账说成存上了）
        Ld = Ledger.load(snap, journal=jp, writer="chk", empty_ok=True)
        if not set(expected) <= _nids(Ld):
            q3.append("③ 恢复存盘之后，注入之前那几节没进快照")

    elif point == "compact_copy_enospc":
        # ── ① 拷底那一下盘满（半份底留在盘上再抛）──
        _boom_copyfile(monkeypatch, jp)
        ret, exc = _call(lambda: L.compact(snap, journal=jp))
        errs, notes["q1"] = _honest(exc, ENOSPC, "compact 拷底", ret)
        q1 += errs
        # ── ② 正本不许被截空（拷底在截空之前失败）、不许留半份底 ──
        q2 += _check_disk(snap, jp, expected, point)
        strays = [n for n in os.listdir(d) if n.endswith(".compact")]
        if strays:
            q2.append(f"② 拷底失败留了半份底 {strays} —— 读的一方（load）会去读它，"
                      f"且没登记过的底永远清不掉")
        # ── ③ 故障解除 ──
        monkeypatch.undo()
        errs3, _ = _recover(snap, jp, expected, point)
        q3 += errs3

    elif point == "lock_create_eacces":
        # ── ① 锁文件建不出来（EACCES）：写者与压实两条路都要给出真原因 ──
        _boom_lock_create(monkeypatch)
        before = open(jp, "rb").read()
        ret, exc = _call(lambda: L.append(jp, K2, 1, "注入那一节", "fp|io", writer="victim"))
        errs, note_a = _honest(exc, EACCES, "append 拿写者锁", ret)
        q1 += errs
        ret2, exc2 = _call(lambda: L.compact(snap, journal=jp))
        errs2, note_c = _honest(exc2, EACCES, "compact 拿压实锁", ret2)
        q1 += errs2
        notes["q1"] = f"append={note_a} · compact={note_c}"
        if open(jp, "rb").read() != before:
            q1.append("① 建锁失败却动了流水（一个字节都不该动）")
        # ── ② 账与锁现场 ──
        q2 += _check_disk(snap, jp, expected, point)
        if any(n.endswith(".lock") for n in os.listdir(d)):
            q2.append("② 建锁失败却在盘上留下了锁文件（没建成就不该有）")
        # ── ③ 故障解除 ──
        monkeypatch.undo()
        errs3, _ = _recover(snap, jp, expected, point)
        q3 += errs3

    else:                                            # pragma: no cover —— POINTS 写错了才到这
        raise AssertionError(f"不认识的注入点 {point}")

    errs = q1 + q2 + q3
    took = time.perf_counter() - t0
    assert not errs, (f"【{point}】IO 错误注入三问没全过（{took:.1f}s）：\n"
                      + "\n".join(errs[:20]))
    print(f"\n[io:{point}] ①报错诚实：{notes.get('q1')} · "
          f"②账不烂：链过/无半行/行数守恒/快照读得动 · "
          f"③恢复：append+save+compact 全通、锁与 tmp 零残留 · {took:.1f}s")
    assert took <= BUDGET_S, f"这一轮 {took:.1f}s（> {BUDGET_S}s 线）"


def test_lock_tells_cant_create_apart_from_dont_own(monkeypatch, tmp_path):
    """锁的两条 `PermissionError` 必须分得开（round 58 的刀口，单进程可复现）。

    锁文件**在**（别人开着 / 占着）→ 照旧让路返回 `None`（round 51 那一行不许破）；
    锁文件**不在**（EACCES / 盘满，是我建不出来）→ 带 errno 如实抛，不许把磁盘故障
    说成「有人占着」。
    """
    lock = str(tmp_path / "book.jsonl.lock")
    real_open = os.open

    def denied(path, flags, *args, **kwargs):
        if str(path) == lock:
            raise PermissionError(EACCES, os.strerror(EACCES), path)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", denied)
    monkeypatch.setattr(time, "sleep", lambda _s: None)   # 建不出来的重试不真等

    # ① 锁文件在：这是「这把锁不归你」，让路，异常不许穿过去
    with open(lock, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    assert Ledger._lock(lock) is None, "别人占着的锁该让路，不该抛"

    # ② 锁文件不在：这是我建不出来，如实抛（errno + 说得出是哪一步），且不许留下锁
    os.unlink(lock)
    ret, exc = _call(lambda: Ledger._lock(lock))
    assert exc is not None, "锁建不出来却返回 None —— 调用方会把它当成「有人占着」"
    assert isinstance(exc, OSError) and exc.errno == EACCES, \
        f"带不出真原因：{type(exc).__name__}[errno={getattr(exc, 'errno', None)}] {exc}"
    assert "建不出来" in str(exc), f"说辞没说清是哪一步：{exc}"
    assert not os.path.exists(lock), "建不出来的锁却留在了盘上"

    # ③ 撤掉故障：同一把锁照旧拿得到（恢复得了吗）
    monkeypatch.undo()
    fd = Ledger._lock(lock)
    assert fd is not None, "故障解除后锁还是建不出来"
    os.close(fd)
    assert Ledger._drop_lock(lock), "拿完没删掉锁"


if __name__ == "__main__":
    raise SystemExit("跑法：python -m pytest -q tests/test_io_errors.py")

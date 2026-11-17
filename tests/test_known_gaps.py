# -*- coding: utf-8 -*-
"""README「还没做的 / Known gaps」逐行清尾（round 51）留下的钉子。

一张表里 26 行，每行要么**被钉住**（这里或既有测试 / 体检），要么**写死为什么留着、
什么条件下关**（README 第三列）。这个文件只放"以前只有文档承诺、没有测试"的那几行 ——
钉住之后，那一行才算从"我们说的"变成"跑出来的"。

每条 docstring 开头就是它在 README 表里的那一行，表改了这里要跟着改。
"""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ledger import Ledger, addr  # noqa: E402

K = addr("Known gaps 钉子")


# ── 行：拿到锁之前可能被 PermissionError 挡一下 ──────────────────────
def test_lock_yields_instead_of_raising_on_permission_error(monkeypatch, tmp_path):
    """Windows 上别人正开着锁文件时，O_EXCL 报 PermissionError 而不是"已存在"。

    两种都得当"这把锁不归你"：照旧让路。只接 FileExistsError 的话，
    PermissionError 会一路穿透到调用方 —— 写者当场抛（README 这一行写的就是这个）。
    """
    lock = str(tmp_path / "book.jsonl.lock")
    with open(lock, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))                 # 活人的锁：本来就该让路

    real_open = os.open

    def guarded(path, flags, *args, **kwargs):
        if str(path) == lock:
            raise PermissionError(13, "别人正开着这个锁文件")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded)
    assert Ledger._lock(lock) is None, "被 PermissionError 挡住时该让路，不是让异常穿过去"


# ── 行：写者要等压实，但有上限 ────────────────────────────────────────
def test_writer_waiting_on_a_lock_raises_instead_of_swallowing(monkeypatch, tmp_path):
    """写者与压实共用 `<流水>.lock`：等不到就如实抛，不吞、不假装写成功。

    等待上限是 3000 × 0.02s = 60 秒（README 写死的那个数）。测试里把 sleep 摸成空操作，
    钉的是**行为**（抛、带路径、行没写进去），不是那 60 秒本身 —— 没人该为一条测试等一分钟。
    """
    jp = str(tmp_path / "book.jsonl")
    led = Ledger(writer="w", ttl=3600)
    led.append(jp, K, 1, "对", "fp")

    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    lock = jp + ".lock"
    try:
        with open(lock, "w", encoding="utf-8") as f:
            f.write(str(holder.pid))               # 别人的活锁：我得等它
        monkeypatch.setattr(ledger_time_module(), "sleep", lambda _s: None)
        before = sum(1 for _ in open(jp, encoding="utf-8") if _.strip())
        with pytest.raises(OSError) as exc:
            led.append(jp, addr("等不到的那一行"), 1, "对", "fp")
        assert lock in str(exc.value), "抛的时候要指名道姓是哪把锁"
        after = sum(1 for _ in open(jp, encoding="utf-8") if _.strip())
        assert after == before, "等不到就不许留下半行账"
    finally:
        holder.kill()
        holder.wait()


def ledger_time_module():
    import ledger
    return ledger.time


# ── 行：`due()` 只报到期的，不替你收口 ───────────────────────────────
def test_due_reports_but_never_resolves():
    """到点了 `due()` 给你清单，改不改判、什么时候收是你的决定。

    账本不许把"还没判"改成 0 —— 报了不等于收了，这一行钉的就是这条分界。
    """
    now = [1000.0]
    led = Ledger(writer="t", now=lambda: now[0])
    led.open_pending(K, why="evidence", need="缺车架号", decide_by=1060.0)

    assert led.due() == []                        # 没到点：连报都不报
    now[0] = 1100.0
    assert len(led.due()) == 1                    # 到点了：报
    assert led.stat.get("resolved", 0) == 0        # 报 ≠ 收
    assert led.stat.get("forced", 0) == 0
    assert led.by_pending(K) is not None           # 悬位原封不动
    assert len(led.due()) == 1                    # 再报一遍，还是同一个悬位


# ── 行：快照多进程会有"后写的赢" ─────────────────────────────────────
def test_two_savers_both_survive_a_reload(tmp_path):
    """两个进程同时 save 同一本账：最后落地那份是赢家，**但不会丢账** —— 流水是正本，
    重读时没进快照的行会被补回来。别拿快照当并发写入口，账却要一直在。
    """
    p = str(tmp_path / "book.json")
    jp = p + ".jsonl"

    a = Ledger(writer="A", ttl=3600)
    a.append(jp, addr("A 写的"), 1, "对", "fp|A")
    b = Ledger(writer="B", ttl=3600)
    b.append(jp, addr("B 写的"), 1, "对", "fp|B")     # 两行都在正本里

    a.save(p, journal=jp)
    b.save(p, journal=jp)                            # 后写的那份落地

    back = Ledger.load(p, writer="r", ttl=3600, journal=jp)
    assert addr("A 写的") in back.nodes, "先写的那份被后写的吃掉了"
    assert addr("B 写的") in back.nodes, "后写的没落住"


# ── 行：`.compact` 备份只增不删 → round 52 关成「保留最近 N 份」 ─────────
def _press(tmp_path, times, keep="默认"):
    """同一本账上连压 times 次，返回 (账, 每次的底名)。`keep="默认"` = 不传这个参数。"""
    p = str(tmp_path / "book.json")
    jp = p + ".jsonl"
    led = Ledger(writer="w", ttl=3600)
    led.append(jp, K, 1, "对", "fp")
    led.save(p, journal=jp)
    baks = []
    for i in range(times):
        led.append(jp, addr(f"第 {i} 次压实"), 1, "对", "fp")
        if keep == "默认":
            baks.append(led.compact(p, journal=jp))
        else:
            baks.append(led.compact(p, journal=jp, keep=keep))
    return led, baks


def _on_disk(tmp_path):
    return [n for n in os.listdir(tmp_path) if n.endswith(".compact")]


def test_compaction_backups_keep_n_default_is_ten(tmp_path):
    """三件套的**默认 + 说明**：默认留最近 10 份，数写在 `Ledger.KEEP_COMPACT_BACKUPS` 上。

    默认值必须是一个真数（不是「不删」）：底是压实的凭据，得留够回滚排障的量。
    """
    assert Ledger.KEEP_COMPACT_BACKUPS == 10, "默认份数改了，README / LEDGER 的说明要跟着改"

    led, baks = _press(tmp_path, 3)                 # 不传 keep = 用默认
    assert all(b and os.path.exists(b) for b in baks), "压实底没留住"
    assert len(_on_disk(tmp_path)) == 3, f"默认 10 份，压 3 次一份都不该删：{_on_disk(tmp_path)}"


def test_compaction_backups_keep_n_prunes_the_oldest(tmp_path):
    """三件套的**可调**：`keep=2` 压 5 次只剩最新 2 份 —— 留新删旧，返回值那份一定还在。"""
    led, baks = _press(tmp_path, 5, keep=2)

    on_disk = _on_disk(tmp_path)
    assert len(on_disk) == 2, f"keep=2 只该剩 2 份：{on_disk}"
    newest_two = sorted(Ledger._backup_stamp(os.path.basename(b)) for b in baks[-2:])
    assert sorted(Ledger._backup_stamp(n) for n in on_disk) == newest_two, "留下的不是最新那两份"
    assert os.path.exists(baks[-1]), "刚压出来的那份底被删了 —— 这次压实的凭据没了"
    assert led.stat["backup_pruned"] == 3, "删了几份要在账上看得见"


def test_compaction_backups_keep_none_keeps_everything(tmp_path):
    """`keep=None` = 一份都不删（round 51 之前的老行为），明写才拿得到。"""
    led, baks = _press(tmp_path, 4, keep=None)
    assert len(_on_disk(tmp_path)) == 4, f"keep=None 不许删：{_on_disk(tmp_path)}"
    assert led.stat.get("backup_pruned", 0) == 0
    assert all(os.path.exists(b) for b in baks)


def test_compaction_backups_keep_rejects_junk(tmp_path):
    """0、负数、字符串、浮点一律当场 ValueError —— 挡在抢锁之前，且一次都没压实。"""
    p = str(tmp_path / "book.json")
    jp = p + ".jsonl"
    led = Ledger(writer="w", ttl=3600)
    led.append(jp, K, 1, "对", "fp")
    led.save(p, journal=jp)

    for bad in (0, -1, "3", 2.0, True):
        with pytest.raises(ValueError):
            led.compact(p, journal=jp, keep=bad)

    assert led.stat.get("compacted", 0) == 0, "参数不对还压了实"
    assert _on_disk(tmp_path) == [], "参数不对就动手 = 抢了锁才报错"


def test_compaction_backups_with_a_tail_are_not_pruned(tmp_path):
    """**删得掉才删**：登记之后又长出尾巴的底（还有半行没进快照），keep=1 也留着。

    另外钉住：别人改名出来的 `xxx.1.compact` 不是我们起的名，碰都不碰。
    """
    p = str(tmp_path / "book.json")
    jp = p + ".jsonl"
    led = Ledger(writer="w", ttl=3600)
    led.append(jp, K, 1, "对", "fp")
    led.save(p, journal=jp)
    foreign = jp + ".1.compact"                     # 体检「压实不丢改名窗口」就造过这种
    open(foreign, "w", encoding="utf-8").close()

    first = led.compact(p, journal=jp, keep=1)
    with open(first, "a", encoding="utf-8", newline="") as f:
        f.write('{"t": "record", "key"')            # 半行：写的人还没写完，还会长

    led.append(jp, addr("再来一节"), 1, "对", "fp")
    second = led.compact(p, journal=jp, keep=1)

    on_disk = _on_disk(tmp_path)
    assert os.path.basename(first) in on_disk, "还有半行没进快照的底被删了"
    assert os.path.basename(second) in on_disk, "刚压出来的那份底被删了"
    assert os.path.basename(foreign) in on_disk, "别人改名出来的文件被当成我们的底删了"
    assert led.stat.get("backup_pruned", 0) == 0, "一份都不许删（都删不得）"


# ── 行：`by_round` 和 `six_port` 的「④圈」同名不同义 ─────────────────
def test_by_round_and_six_port_round_are_different_things():
    """前者返**整串圈号**，后者是"这一节的圈号"。读的时候别混用 —— 钉住两种形状。"""
    led = Ledger(writer="t", ttl=3600)
    led.record(K, 1, "第一圈", "fp")
    led.record(K, 0, "第二圈", "fp")

    assert led.by_round(K) == [1, 2]                # 整串
    ports, _ = led.six_port(K, "fp")
    assert ports["④圈"] == 2                        # 只是最新那一节
    ports_first, _ = led.six_port(K, "fp", rnd=1)
    assert ports_first["④圈"] == 1                  # 指名道姓才拿得到别的节

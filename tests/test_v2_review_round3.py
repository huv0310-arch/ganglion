# -*- coding: utf-8 -*-
"""第三轮评审挖出的三处：改历史的号、压得勤丢行、删锁失败永久让路。

都是"并发/重入"这一类毛病 —— 单进程顺着走一遍全绿，两个人一起走就丢账。
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ledger as L


def test_new_process_does_not_renumber_disk(tmp_path):
    """没读过盘的新账直接 save：盘上那几节的圈号一个字都不许动。

    以前按"谁是自己"决定谁让号 —— 从新进程这边看，让号的成了盘上的历史，
    同一次合并从两边各看一遍结论不同，这就是"历史被改"的来源。
    现在按 ts 定：先判的保住号，后判的顺延。
    """
    p = str(tmp_path / "book.json")
    jp = p + ".jsonl"
    old = L.Ledger(writer="A", ttl=3600)
    for i in (1, 2, 3):
        old.record("k", 1, f"A-{i}", "fp|A", writer="A")
    old.save(p, journal=jp)

    new = L.Ledger(writer="B", ttl=3600)
    for i in (1, 2):
        new.record("k", 0, f"B-{i}", "fp|B", writer="B")
    new.save(p, journal=jp)

    back = L.Ledger.load(p, writer="r", ttl=3600)
    assert [(n["round"], n["reason"]) for n in back.nodes["k"]] == [
        (1, "A-1"), (2, "A-2"), (3, "A-3"), (4, "B-1"), (5, "B-2")]

    back.probe("k", "fp|A", checked=True, journal=jp)
    again = L.Ledger.load(p, writer="r2", ttl=3600)
    hit = [(n["round"], n["reason"]) for n in again.nodes["k"] if n["hits"]]
    assert hit == [(3, "A-3")], f"借票落到别人头上了：{hit}"


def test_writers_survive_compact(tmp_path):
    """一边写一边压：写的人不许被打断，行也不许丢。

    以前 Windows 上压实改名那几十毫秒里，写者 open 会抛 PermissionError /
    FileNotFoundError，线程当场死掉 —— 账面看不出来，就是少了几条。
    """
    p = str(tmp_path / "book.json")
    jp = p + ".jsonl"
    seed = L.Ledger(writer="seed", ttl=3600)
    seed.append(jp, "seed", 1, "起", "fp")
    seed.save(p, journal=jp)

    errors = []

    def writer(i):
        led = L.Ledger(writer=f"w{i}", ttl=3600)
        for n in range(10):
            try:
                led.append(jp, f"k{i}-{n}", 1, f"第{n}条", "fp")
            except Exception as e:            # noqa: BLE001
                errors.append(repr(e)[:60])
                return

    ths = [threading.Thread(target=writer, args=(i,)) for i in range(3)]
    for t in ths:
        t.start()
    for _ in range(5):
        L.Ledger(writer="C", ttl=3600).compact(p, journal=jp)
    for t in ths:
        t.join()

    assert not errors, f"写者被压实打断了：{errors[0]}"
    back = L.Ledger.load(p, writer="r", ttl=3600)
    want = {f"k{i}-{n}" for i in range(3) for n in range(10)} | {"seed"}
    assert not (want - set(back.nodes)), f"丢了：{sorted(want - set(back.nodes))}"


def test_backup_names_never_collide(tmp_path):
    """同一毫秒压两次，备份不许互相盖 —— 盖掉的那份里的行还没进任何快照。"""
    p = str(tmp_path / "book.json")
    jp = p + ".jsonl"
    led = L.Ledger(writer="w", ttl=3600)
    led.append(jp, "k", 1, "对", "fp")
    led.save(p, journal=jp)

    names = set()
    for i in range(6):
        led.append(jp, f"k{i}", 1, f"第{i}条", "fp")      # 每轮都造出新流水
        got = L.Ledger(writer="C", ttl=3600).compact(p, journal=jp)
        assert got, "压不动了"
        base = os.path.basename(got)
        assert base not in names, f"备份撞名：{base}"
        names.add(base)

    back = L.Ledger.load(p, writer="r", ttl=3600)
    assert set(back.nodes) == {"k", "k0", "k1", "k2", "k3", "k4", "k5"}

def test_self_leftover_lock_is_cleared(tmp_path):
    """我自己上一轮删锁失败留下的锁：下一轮必须能清，不许一辈子让路。"""
    p = str(tmp_path / "book.json")
    jp = p + ".jsonl"
    led = L.Ledger(writer="w", ttl=3600)
    led.append(jp, "k", 1, "对", "fp")
    led.save(p, journal=jp)

    lock = p + ".compact.lock"
    with open(lock, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))          # 锁里是我的 pid：看起来像活锁
    led._left_lock = lock                  # 只有我知道这是我上一轮没删掉的

    assert led.compact(p, journal=jp), "自己的残渣锁把自己挡死了"
    assert not os.path.exists(lock)


def test_other_live_lock_still_yields(tmp_path):
    """别人的活锁：我桌上没有这条记录，照旧让路。"""
    p = str(tmp_path / "book.json")
    jp = p + ".jsonl"
    led = L.Ledger(writer="w", ttl=3600)
    led.append(jp, "k", 1, "对", "fp")
    led.save(p, journal=jp)

    lock = p + ".compact.lock"
    # 别人的**活**锁：真起一个活进程拿着它（以前图省事写自己的 pid，
    # 但"我自己的 pid 却没握在手里"正是"我的残渣"的样子 —— 两件事必须用两种现场来验）
    import subprocess, sys, time as _t
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with open(lock, "w", encoding="utf-8") as f:
            f.write(str(holder.pid))
        assert L.Ledger(writer="C", ttl=3600).compact(p, journal=jp) is False
    finally:
        holder.kill()
        holder.wait()

def test_reverse_merge_keeps_chain_monotonic(tmp_path):
    """反向合并（后写者的账比盘上的晚）也要让整条链按时间排，翻面数不许虚增。

    只把撞号那节顺延会把后写者的链切开：冷读 (1,B-1)(2,B-2)(3,A-3)(4,A-1)(5,A-2)，
    时间倒流，flips 多报一次"没发生过的改判"。
    """
    p = str(tmp_path / "book.json")
    jp = p + ".jsonl"
    b = L.Ledger(writer="B", ttl=3600)
    b.record("k", 1, "B-1", "fp|B", writer="B")
    b.record("k", 0, "B-2", "fp|B", writer="B")
    b.save(p, journal=jp)

    a = L.Ledger(writer="A", ttl=3600)
    for i, v in enumerate((0, 0, 1), 1):
        a.record("k", v, f"A-{i}", "fp|A", writer="A")
    a.save(p, journal=jp)

    back = L.Ledger.load(p, writer="r", ttl=3600)
    chain = back.nodes["k"]
    assert [(n["round"], n["reason"]) for n in chain] == [
        (1, "B-1"), (2, "B-2"), (3, "A-1"), (4, "A-2"), (5, "A-3")]
    ts = [n["ts"] for n in chain]
    assert all(x <= y for x, y in zip(ts, ts[1:])), "链上时间倒流"
    assert len(back.flips("k")) == 2, "翻面数虚增（时报到没发生过的改判）"



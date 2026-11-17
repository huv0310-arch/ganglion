# -*- coding: utf-8 -*-
"""第八轮（锁自愈）的现场：空锁、死 pid、续命要续两把。"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ledger as L
from ledger import Ledger


def test_lock_content_always_carries_our_pid(tmp_path):
    p = str(tmp_path / "book.json")
    jp = p + ".jsonl"
    Ledger(writer="t", ttl=3600).save(p, journal=jp)
    Ledger(writer="t", ttl=3600).append(jp, "k", 1, "写一行", "fp|A")
    # 写完之后锁不该留在盘上；万一留下，里面必须有能认的 pid —— 0 字节锁
    # 谁都接管不了（第八轮口子甲）
    assert not os.path.exists(jp + ".lock")


def test_dead_pid_lock_is_taken_over_at_once(tmp_path):
    p = str(tmp_path / "book.json")
    jp = p + ".jsonl"
    Ledger(writer="t", ttl=3600).save(p, journal=jp)
    with open(jp + ".lock", "w", encoding="utf-8") as f:
        f.write("999999")
    t0 = time.time()
    Ledger(writer="t", ttl=3600).append(jp, "k", 1, "残锁之后", "fp|A")
    assert time.time() - t0 < 2, "主人已死的锁不该让写者排队等"


def test_keep_lock_warm_refreshes_every_lock_it_is_given(tmp_path):
    """压实同时握着压实锁和写者锁，两把都得续命 —— 只暖一把，
    另一把会被 mtime 兜底接管，thief 正好在截空时插进来（第八轮发现）。"""
    a = str(tmp_path / "a.lock")
    b = str(tmp_path / "b.lock")
    for path in (a, b):
        open(path, "w").close()
        old = time.time() - 3600
        os.utime(path, (old, old))
    stop = threading.Event()
    th = threading.Thread(target=Ledger._keep_lock_warm, args=((a, b), stop, 0.05), daemon=True)
    th.start()
    time.sleep(0.25)
    stop.set()
    th.join(2)
    for path in (a, b):
        assert time.time() - os.path.getmtime(path) < 10, path

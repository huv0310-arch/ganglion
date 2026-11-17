# -*- coding: utf-8 -*-
"""协作者 B评审（2026-10-31）挖出的三个坑，逐条钉住。

① compact 的改名窗口会静默丢账
② _replay_node 的幂等判据只有 (ts, verdict)，两头都错 → 伪造翻面
③ probe() 原地改 hits 不进流水 → 多进程丢更新，prune() 借得越多忘得越快

这些测试在修之前**应该全红**。留着它们，是为了以后谁顺手改回去时，
有人当场被拦住。
"""
import json
import os
import time
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from ledger import Ledger, addr  # noqa: E402


# ── ② 重演幂等：判据是内容，不是 (ts, verdict) ─────────────────────────

def _node(round_, ts, verdict, reason="对"):
    return {"key": "k1", "verdict": verdict, "reason": reason, "ts": ts, "round": round_,
            "value_score": None, "ctx_fp": "fp|A", "hits": 0, "last_hit": None,
            "borrows": [], "ttl": 3600, "writer": "t", "tombstone": None}


def test_same_ts_different_round_kept():
    """同 ts、同判决，但是两圈 —— 旧判据会把它当重复丢掉。"""
    led = Ledger(writer="t", ttl=3600)
    led._replay_node("k1", _node(1, 100.0, 1))
    led._replay_node("k1", _node(2, 100.0, 1))     # 同一毫秒判了两次，确实是两圈
    assert len(led.nodes["k1"]) == 2


def test_same_round_different_content_does_not_duplicate_round_number():
    """同圈号但内容不同 —— 旧判据会 append 成 [1,2,3,2]，凭空造出一次翻面。"""
    led = Ledger(writer="t", ttl=3600)
    led._replay_node("k1", _node(1, 100.0, 1))
    led._replay_node("k1", _node(2, 101.0, 0))
    led._replay_node("k1", _node(2, 102.0, 1))     # 圈号 2 被占了，别的内容来了
    rounds = [n["round"] for n in led.nodes["k1"]]
    assert rounds == sorted(rounds), f"圈号乱了：{rounds}"
    assert len(set(rounds)) == len(rounds), f"圈号重复：{rounds}"


def test_exact_duplicate_is_skipped_even_after_hits():
    """同一节重演两次不许记两遍 —— 哪怕它已经被借过（hits 是统计，不是内容）。"""
    led = Ledger(writer="t", ttl=3600)
    n = _node(1, 100.0, 1)
    led._replay_node("k1", n)
    led.nodes["k1"][0]["hits"] = 3
    led._replay_node("k1", n)
    assert len(led.nodes["k1"]) == 1
    assert led.nodes["k1"][0]["hits"] == 3


# ── ③ 借这一下进流水 ───────────────────────────────────────────────────

def test_probe_with_journal_is_durable(tmp_path):
    p = str(tmp_path / "book.json")
    jp = p + ".jsonl"
    a = Ledger(writer="a", ttl=3600)
    a.append(jp, addr("这节会被借"), 1, "对", "fp|A")
    a.save(p)

    b = Ledger.load(p, writer="b", ttl=3600)
    node = b.probe(addr("这节会被借"), "fp|A", checked=True, journal=jp)
    assert node is not None and node["hits"] == 1
    b.save(p)

    c = Ledger.load(p, writer="c", ttl=3600)       # 第三个人读回来
    assert c.nodes[addr("这节会被借")][0]["hits"] == 1, "借这一下没落盘"


def test_two_borrowers_do_not_overwrite_each_other(tmp_path):
    """两个进程同时借同一节：旧写法各改各的内存，最后只有一个人算数。"""
    p = str(tmp_path / "book.json")
    jp = p + ".jsonl"
    a = Ledger(writer="a", ttl=3600)
    a.append(jp, addr("热门的一节"), 1, "对", "fp|A")
    a.save(p)

    x = Ledger.load(p, writer="x", ttl=3600)
    y = Ledger.load(p, writer="y", ttl=3600)       # 两个人都从同一份快照出发
    x.probe(addr("热门的一节"), "fp|A", journal=jp)
    y.probe(addr("热门的一节"), "fp|A", journal=jp)
    x.save(p)

    z = Ledger.load(p, writer="z", ttl=3600)
    assert z.nodes[addr("热门的一节")][0]["hits"] == 2, "两次借用只记到一次"


def test_prune_sees_journal_borrows(tmp_path):
    """借得越多越不许忘：prune 靠 hits 判生死，hits 得从流水里来。

    第一圈被判过、没人翻、也不是最后一圈 —— 照规矩该褪成梗概。
    但只要它在流水里被借过一次，prune 就必须留住它（鲜活档）。
    """
    p = str(tmp_path / "book.json")
    jp = p + ".jsonl"
    a = Ledger(writer="a", ttl=3600)
    k = addr("借过的别忘")
    a.append(jp, k, 1, "第一圈", "fp|A")
    a.append(jp, k, 1, "第二圈", "fp|A")
    a.save(p)

    plain = Ledger.load(p, writer="c", ttl=3600)
    plain.prune(key=k, keep_last=1)
    assert plain.nodes[k][0].get("demoted") is True, \
        "对照组：没被借过的第一圈本来就该褪成梗概"
    assert plain.nodes[k][1].get("demoted") is not True

    # 另一个进程借了第一圈（借是谁借的、借的哪一圈，都写在流水里）
    Ledger(writer="b")._write_line(jp, {"t": "hit", "key": k, "round": 1,
                                        "ts": 1793439999.0, "checked": False, "by": "b"})
    got = Ledger.load(p, writer="c", ttl=3600)
    assert got.nodes[k][0]["hits"] == 1, "流水里那一借没进账"

    got.prune(key=k, keep_last=1)
    assert got.nodes[k][0].get("demoted") is not True, \
        "被借过的节被褪了（借得越多忘得越快）"
    assert got.nodes[k][0]["hits"] == 1
    assert len([n for n in got.nodes[k] if not got.is_tombstone(n)]) == 2


# ── ① 压实窗口 + 安全网 ────────────────────────────────────────────────

def test_compact_keeps_counts_stable_across_rounds(tmp_path):
    p = str(tmp_path / "book.json")
    jp = p + ".jsonl"
    led = Ledger(writer="w", ttl=3600)
    for i in range(3):
        led.append(jp, addr(f"第{i}节"), 1, "对", "fp|A")
    led.compact(p)
    for _ in range(3):
        Ledger.load(p, writer="w", ttl=3600).compact(p)
    back = Ledger.load(p, writer="w", ttl=3600)
    assert len(back.nodes) == 3, f"压实把账弄丢/弄重了：{len(back.nodes)}"


def test_crash_after_rename_still_recovers(tmp_path):
    """压到一半崩了（改名完成、快照没来得及补）：下次读要把备份里的行找回来。

    模拟：快照写好 → 又写一行 → 只把这行改名成 .compact（快照里没有、也没登记）。
    """
    p = str(tmp_path / "book.json")
    jp = p + ".jsonl"
    led = Ledger(writer="w", ttl=3600)
    led.append(jp, addr("崩之前那节"), 1, "对", "fp|A")
    led.save(p)

    late = Ledger(writer="w")
    late.append(jp, addr("窗口里撞进来的"), 0, "没写进快照", "fp|B")
    bak = jp + ".1.compact"
    os.replace(jp, bak)                    # 崩在这里：改名走了，快照没补

    back = Ledger.load(p, writer="w", ttl=3600)
    assert addr("窗口里撞进来的") in back.nodes, "备份里的行没人管，静默丢了"
    assert addr("崩之前那节") in back.nodes


def test_consumed_backup_is_not_replayed_twice(tmp_path):
    p = str(tmp_path / "book.json")
    jp = p + ".jsonl"
    led = Ledger(writer="w", ttl=3600)
    led.append(jp, addr("只该有一节"), 1, "对", "fp|A")
    bak = led.compact(p)
    assert bak and os.path.exists(bak)
    for _ in range(3):
        back = Ledger.load(p, writer="w", ttl=3600)
        back.save(p)
    final = Ledger.load(p, writer="w", ttl=3600)
    assert len(final.nodes[addr("只该有一节")]) == 1, "压实备份被重复吃进去"


# ── ④ 借票不许悄悄不落账（协作者 B第二轮评审，2026-10-31） ────────────────

def test_probe_requires_journal(tmp_path):
    """不传 journal 借不出来 —— 借票必须落账，没有默认值。"""
    p = str(tmp_path / "book.json")
    jp = p + ".jsonl"
    led = Ledger(writer="t", ttl=3600)
    k = addr("会借的一节")
    led.append(jp, k, 1, "对", "fp|A")
    led.save(p)

    b = Ledger.load(p, writer="t", ttl=3600)
    try:
        b.probe(k, "fp|A")
    except TypeError:
        pass
    else:
        raise AssertionError("不传 journal 也借得出来：借票悄悄不落账")

    c = Ledger.load(p, writer="t", ttl=3600)
    try:
        c.probe(k, "fp|A", journal=None)
    except ValueError:
        pass
    else:
        raise AssertionError("journal=None 也借得出来")


def test_journal_false_means_no_log(tmp_path):
    """journal=False 是"明说不落账"：内存里算，流水里不写。"""
    p = str(tmp_path / "book.json")
    jp = p + ".jsonl"
    led = Ledger(writer="t", ttl=3600)
    k = addr("借了但不落账的一节")
    led.append(jp, k, 1, "对", "fp|A")
    led.save(p)
    before = os.path.getsize(jp)

    b = Ledger.load(p, writer="t", ttl=3600)
    assert b.probe(k, "fp|A", journal=False) is not None
    assert b.nodes[k][0]["hits"] == 1
    assert os.path.getsize(jp) == before, "journal=False 偷偷写了流水"

    back = Ledger.load(p, writer="t", ttl=3600)
    assert back.nodes[k][0]["hits"] == 0


def test_ganglion_check_logs_its_borrow(tmp_path):
    """骨+账的入口也会借 —— 它借的那张票必须进流水（协作者 B：钥匙要插门上）。"""
    from ledger import LedgerGanglion
    p = str(tmp_path / "book.json")
    jp = p + ".jsonl"
    led = Ledger(writer="t", ttl=3600)
    text = "这台车手续齐吗"
    led.append(jp, addr(text), 1, "齐", "fp|A")
    led.save(p)

    g = LedgerGanglion(Ledger.load(p, writer="t", ttl=3600), journal=jp)
    g.check(text, ctx_fp="fp|A")
    lines = [json.loads(l) for l in open(jp, encoding="utf-8") if l.strip()]
    assert any(x.get("t") == "hit" for x in lines), "check() 借了账却没落票"

    back = Ledger.load(p, writer="t", ttl=3600)
    assert back.nodes[addr(text)][0]["hits"] == 1


def test_ganglion_requires_journal():
    """没有地方落票，就别把骨和账焊在一起。"""
    from ledger import LedgerGanglion
    try:
        LedgerGanglion(Ledger(writer="t", ttl=3600))
    except ValueError:
        return
    raise AssertionError("LedgerGanglion 可以不给 journal 就构造出来")


# ── ⑤ 压实要拿锁（两个压实者互相抽纸） ───────────────────────────────

def test_compact_gives_way_when_locked(tmp_path):
    p = str(tmp_path / "book.json")
    jp = p + ".jsonl"
    lock = p + ".compact.lock"
    led = Ledger(writer="t", ttl=3600)
    led.append(jp, addr("第一节"), 1, "对", "fp|A")
    led.save(p)

    # 空锁**不是**"别人正拿着"：空锁是垃圾锁（有人建完文件就死了），压实会等它 5 秒再接管。
    # 要验"让路"，得真放一个活人上去拿着。
    import subprocess, sys
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with open(lock, "w", encoding="utf-8") as f:
            f.write(str(holder.pid))
        assert Ledger.load(p, writer="t", ttl=3600).compact(p) is False
        assert os.path.exists(jp), "让路那次把流水动了"
    finally:
        holder.kill()
        holder.wait()

    os.unlink(lock)
    assert Ledger.load(p, writer="t", ttl=3600).compact(p), "锁放开后压不动"
    assert not os.path.exists(lock), "压完没放锁"


def test_save_beside_a_reader_does_not_crash(tmp_path):
    """Windows：有人正读快照时改名会 WinError 5 —— 要等，不许崩也不许装作存上了。"""
    import threading
    p = str(tmp_path / "book.json")
    jp = p + ".jsonl"
    led = Ledger(writer="t", ttl=3600)
    led.append(jp, addr("一节"), 1, "对", "fp|A")
    led.save(p)

    err = []
    stop = {"v": False}

    def reader():
        try:
            while not stop["v"]:
                Ledger.load(p, writer="r", ttl=3600)
        except Exception as e:      # noqa: BLE001
            err.append(e)

    t = threading.Thread(target=reader)
    t.start()
    try:
        w = Ledger(writer="t", ttl=3600)
        for i in range(30):
            w.append(jp, addr(f"边读边写{i}"), 1, "对", "fp|A")
            Ledger.load(p, writer="t", ttl=3600).save(p)
    finally:
        stop["v"] = True
        t.join()
    assert not err, f"读的人被崩了：{err[:1]}"
    assert len(Ledger.load(p, writer="t", ttl=3600).nodes) >= 30


# ── ⑥ 协作者 B第三轮：让路说清楚 / 收口落账 / 活锁不许抢 ──────────────────

def test_resolve_requires_journal(tmp_path):
    p = str(tmp_path / "book.json")
    led = Ledger(writer="t")
    k = addr("到期没等到料")
    led.open_pending(k, why="missing", need="补件", ctx_fp="fp|A", decide_by=0)
    led.save(p)

    b = Ledger.load(p, writer="t")
    try:
        b.resolve(k, forced=True)
    except TypeError:
        pass
    else:
        raise AssertionError("不传 journal 也收得掉：强收口悄悄不进流水")

    c = Ledger.load(p, writer="t")
    c.resolve(k, None, "到点没等到料", journal=p + ".jsonl", forced=True)
    line = [json.loads(x) for x in open(p + ".jsonl", encoding="utf-8") if x.strip()][-1]
    assert line["t"] == "resolve" and line["forced"] is True
    assert line["by"] == "t", "收口没署名"


def test_compact_does_not_wipe_other_writers(tmp_path):
    """手上那份比磁盘旧时，压实不许拿旧账盖新快照。"""
    p = str(tmp_path / "book.json")
    jp = p + ".jsonl"
    Ledger(writer="t", ttl=3600).save(p)
    stale = Ledger.load(p, writer="t", ttl=3600)      # 先攥一份旧的

    w = Ledger(writer="w", ttl=3600)
    for i in range(5):
        w.append(jp, addr(f"别人写的{i}"), 1, "对", "fp|A")
    Ledger.load(p, writer="w", ttl=3600).compact(p)   # 别人先压过一轮

    stale.compact(p)                                    # 旧的那份再来压
    back = Ledger.load(p, writer="t", ttl=3600)
    missing = [i for i in range(5) if addr(f"别人写的{i}") not in back.nodes]
    assert not missing, f"旧内存把别人的账盖掉了：缺 {missing}"


def test_live_lock_is_not_stolen(tmp_path):
    """压得久不是烂锁：锁主人还活着，mtime 再老也不许抢。"""
    p = str(tmp_path / "book.json")
    led = Ledger(writer="t", ttl=3600)
    led.append(p + ".jsonl", addr("一节"), 1, "对", "fp|A")
    led.save(p)
    lock = p + ".compact.lock"
    import subprocess, sys
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with open(lock, "w", encoding="utf-8") as f:
            f.write(str(holder.pid))                   # 真活着的外人（不是自己的 pid：
                                                      # "我的 pid 却没握在手里"＝我的残渣）
        # mtime 新鲜才是"真活着"：压实的持锁者每 15 秒续一次 mtime，
        # 一把活锁的 mtime 绝不会老过 45 秒。老成那样只可能没人握着（见下一条断言）。
        assert Ledger.load(p, writer="t", ttl=3600).compact(p) is False
    finally:
        holder.kill()
        holder.wait()

    with open(lock, "w", encoding="utf-8") as f:
        f.write("999999")                              # 主人早没了
    assert Ledger.load(p, writer="t", ttl=3600).compact(p), "死锁也不许接管：这本账永远压不动"

    # pid 看着活着、其实没人握着（pid 被复用 / 进程退了句柄还没释放）：
    # 靠 mtime 兜底 —— 持锁者每 15 秒续命，老过 45 秒就是没人了
    with open(lock, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    old = time.time() - 3600
    os.utime(lock, (old, old))
    assert Ledger.load(p, writer="t", ttl=3600).compact(p), "看起来活着的死锁也要能接管"


# ── ⑧ 写方一直写、压实一直压：尾巴不许被"登记成已消费"藏掉 ────────────

def test_compaction_never_hides_a_tail(tmp_path):
    """写方 open 的时候 jp 还没改名、write 的时候已经叫 bak 了 —— 这一行必须补回来。"""
    import threading
    p = str(tmp_path / "book.json")
    jp = p + ".jsonl"
    Ledger(writer="t", ttl=3600).save(p)
    N = 200
    done = threading.Event()

    def writer():
        w = Ledger(writer="w", ttl=3600)
        for i in range(N):
            w.append(jp, addr(f"边写边压{i}"), 1, "对", "fp|A")
        done.set()

    t = threading.Thread(target=writer)
    t.start()
    while not done.is_set():
        Ledger.load(p, writer="c", ttl=3600).compact(p)
    t.join()
    Ledger.load(p, writer="c", ttl=3600).compact(p)

    back = Ledger.load(p, writer="c", ttl=3600)
    missing = [i for i in range(N) if addr(f"边写边压{i}") not in back.nodes]
    assert not missing, f"压实把尾巴藏掉了：缺 {len(missing)} 节（如 {missing[:3]}）"

# ── ⑨ 外部实测（协作者 C 2026-10-31）：参数错位不许静默写脏账 ───────────────

def test_append_refuses_swapped_args(tmp_path, monkeypatch):
    """少写一位路径时，地址会被当文件名 —— 以前写出一条 key=null 的假账还不报错。"""
    monkeypatch.chdir(tmp_path)
    L = Ledger(writer="t", ttl=3600)
    k = addr("一段正文")
    for args in ((k, None, "缺料", "fp|A"),            # 地址当路径
                 (str(tmp_path / "b.jsonl"), "", 1, "对", "fp|A"),   # key 空
                 (str(tmp_path / "b.jsonl"), None, 1, "对", "fp|A"), # key 不是摘要
                 (str(tmp_path / "b.jsonl"), k, "对", "对", "fp|A")):# 判决是字符串
        with pytest.raises(ValueError):
            L.append(*args)
    assert list(tmp_path.iterdir()) == [], "错位调用留下了文件（脏账落地了）"

    # 正确写法照常
    jp = str(tmp_path / "b.jsonl")
    L.append(jp, k, 1, "对", "fp|A")
    assert os.path.exists(jp)

# ── ⑩ 流水换代后，上一代的偏移不许把新行跳过（DrvFs 上 1/10 复现） ─────────

def test_stale_offset_after_rotation_does_not_skip(tmp_path):
    p = str(tmp_path / "book.json")
    jp = p + ".jsonl"
    a = Ledger(writer="a", ttl=3600)
    for i in range(3):
        a.append(jp, addr(f"旧{i}"), 1, "对", "fp|A")
    a.save(p)

    reader = Ledger.load(p, writer="r", ttl=3600)      # 记住这一代流水读到哪儿
    Ledger.load(p, writer="c", ttl=3600).compact(p)    # 别人压一轮：旧流水改名、新建空流水

    w = Ledger(writer="w", ttl=3600)
    for i in range(3):
        w.append(jp, addr(f"新{i}"), 1, "对", "fp|A")

    reader.compact(p)      # 拿着上一代的偏移来压：新文件比那个偏移短，不能就此当"读过了"

    back = Ledger.load(p, writer="c", ttl=3600)
    missing = [i for i in range(3) if addr(f"新{i}") not in back.nodes]
    assert not missing, f"流水换代后漏了 {len(missing)} 节：{missing}"





# ── ⑦ 协作者 B第四轮：对齐=覆盖会抹掉还没落盘的账 ───────────────────────

def test_compact_keeps_unsaved_in_memory_nodes(tmp_path):
    """我手上还没 save 的一节，压实不许当脏数据扔掉。"""
    p = str(tmp_path / "book.json")
    jp = p + ".jsonl"
    Ledger(writer="t", ttl=3600).save(p)

    mine = Ledger.load(p, writer="t", ttl=3600)
    mine.record(addr("我还没落盘的一节"), 1, reason="对", ctx_fp="fp|A")

    w = Ledger(writer="w", ttl=3600)
    w.append(jp, addr("别人写的"), 1, "对", "fp|A")
    Ledger.load(p, writer="w", ttl=3600).compact(p)    # 别人先压一轮

    mine.compact(p)
    back = Ledger.load(p, writer="t", ttl=3600)
    assert addr("我还没落盘的一节") in back.nodes, "压实把没落盘的账抹掉了"
    assert addr("别人写的") in back.nodes, "压实把别人的账抹掉了"


def test_save_does_not_clobber_newer_snapshot(tmp_path):
    """我读进来的那版早就被改了：存盘要先合并，不许拿旧账盖新账。"""
    p = str(tmp_path / "book.json")
    jp = p + ".jsonl"
    Ledger(writer="t", ttl=3600).save(p)
    mine = Ledger.load(p, writer="t", ttl=3600)

    w = Ledger(writer="w", ttl=3600)
    w.append(jp, addr("别人写的"), 1, "对", "fp|A")
    Ledger.load(p, writer="w", ttl=3600).compact(p)    # 别人压实了：这一节只剩快照里有

    mine.record(addr("我后来写的"), 1, reason="对", ctx_fp="fp|A")
    mine.save(p)
    back = Ledger.load(p, writer="t", ttl=3600)
    assert addr("别人写的") in back.nodes, "存盘把别人的账盖掉了"
    assert addr("我后来写的") in back.nodes




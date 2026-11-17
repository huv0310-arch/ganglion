# -*- coding: utf-8 -*-
"""conformance.py —— 标准件体检。谁把这块搬走，先跑它一遍自证。

    python conformance.py                  # 查当前目录
    python conformance.py D:/path/repo     # 查别人的
    python conformance.py --json           # 给机器看

设计立场
────────
· 只查**规矩**，不查"好不好用"——好不好用要真账才验得出来，这里验不出。
· 红（FAIL）= 违反了写在 README 里的规矩；
  黄（KNOWN）= 我们自己知道还没修、如实暴露的欠账（修好了会自动变绿）。
· 每一项都不许依赖"我们家的业务"：全部在内存 + 临时目录里跑，
  不需要数据库、不联网、不碰任何真实账本。
· 骨节本体（ganglion.py）只验行为，不验哈希：别人 fork 了改注释不算错，
  但把 105 行改胖、把三值改坏，必须红。
"""
import argparse
import ast
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time

PASS, FAIL, KNOWN = "PASS", "FAIL", "KNOWN"
SKIP = "SKIP"        # 这一项**抽不到东西可验**（不是通过，也不是欠账）：如实亮着，不许冒充绿

# 通用件里不该出现的业务词（判的是代码里的标识符，不是注释和字符串）
BUSINESS_WORDS = {
    "dms", "okx", "crypto", "weixin", "wechat", "clawchat",
    "chepai", "plate", "tyre", "tire", "order", "invoice", "settle",
}


def _load(repo):
    """从指定目录加载 ganglion / ledger，避免吃到别处已 import 的同名模块。"""
    sys.path.insert(0, repo)
    mods = {}
    for name in ("ganglion", "ledger"):
        path = os.path.join(repo, name + ".py")
        if not os.path.exists(path):
            raise FileNotFoundError(f"{repo} 下没有 {name}.py")
        spec = importlib.util.spec_from_file_location(f"_conf_{name}", path)
        m = importlib.util.module_from_spec(spec)
        sys.modules[f"_conf_{name}"] = m
        spec.loader.exec_module(m)
        mods[name] = m
    return mods["ganglion"], mods["ledger"]


# ══════════════════════ 各项体检 ══════════════════════
# 每项：fn(G, L) -> (状态, 说明)

def c_v1_bone(G, L):
    """骨节本体：三值原样出去，判一次就忘，异常不塌。"""
    path = G.__file__
    lines = sum(1 for _ in open(path, encoding="utf-8"))
    sha = hashlib.sha256(open(path, "rb").read()).hexdigest()[:16]

    g = G.Ganglion()                       # 无 mem = 不记忆模式
    if g.check("x", lambda c: (1, "yes"))[0] != 1:
        return FAIL, "1 没有原样出去"
    if g.check("x", lambda c: (0, "no"))[0] != 0:
        return FAIL, "0 没有原样出去"
    if g.check("x", lambda c: (None, "?"))[0] is not None:
        return FAIL, "空没有原样出去"
    if g.check("x", lambda c: (_ for _ in ()).throw(RuntimeError("boom")))[0] is not None:
        return FAIL, "processor 抛异常时没有 fail-open 成空"

    if lines > 140:
        return FAIL, f"本体已经 {lines} 行（>140）——往里塞东西了，它开始退化成业务件"
    return PASS, f"{lines} 行 / sha256:{sha}（只报不改，改注释不算错）"


def c_no_defaults(G, L):
    """零默认值：时间、投递、配对全由使用者填。"""
    led = L.Ledger(writer="t")
    zeros = []
    if getattr(led, "ttl", "?") is not None:
        zeros.append(f"ttl={led.ttl!r}")
    if getattr(led, "deliver", "?") is not None:
        zeros.append(f"deliver={led.deliver!r}")
    if zeros:
        return FAIL, "给了默认值：" + ", ".join(zeros) + "（默认值一给，时间本身就没了）"
    return PASS, "ttl / deliver 都是 None：不填就不替你决定"


def c_writer_signed(G, L):
    """谁写的必须署名：没有默认值，且落盘之后还认得出是谁。"""
    bare = L.Ledger()
    try:
        bare.record(L.addr("不署名的一节"), 1, "对", "fp|A")
    except ValueError:
        pass
    else:
        return FAIL, "没署名也把账落了 —— 出事时谁都能说不是我"

    led = L.Ledger(writer="me", ttl=3600)
    n = led.record(L.addr("署了名的一节"), 1, "对", "fp|A")
    if n.get("writer") != "me":
        return FAIL, f"账本声明了写者，节上却是 {n.get('writer')!r}"
    n2 = led.record(L.addr("代写的一节"), 0, "替你记的", "fp|A", writer="helper")
    if n2.get("writer") != "helper":
        return FAIL, "代写不生效：每节必须能覆盖账本级的署名"

    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "book.json")
        led.save(p)
        back = L.Ledger.load(p, ttl=3600)
        if back.writer != "me":
            return FAIL, "落盘丢了账本归属"
        if back.by_verdict(L.addr("署了名的一节")) != 1:
            return FAIL, "读回来找不到那一节"
        if back.nodes[L.addr("署了名的一节")][0].get("writer") != "me":
            return FAIL, "落盘丢了每节的署名"
    return PASS, "不署名不许落账；代写可覆盖；落盘后账本归属与每节署名都还在"


def c_record_only(G, L):
    """只记不借：没声明时间窗的账本，借账必须吵，不许沉默。"""
    led = L.Ledger(writer="t")
    led.record(L.addr("某事"), 1, "通了", "fp|a")
    try:
        led.probe(L.addr("某事"), "fp|a", journal=False)
    except L.LedgerBlocked:
        return PASS, "没 ttl 时借账抛 LedgerBlocked —— 只记不借是明说的"
    return FAIL, "没 ttl 也把账借出去了（或沉默返回），规矩被默认掉了"


def c_empty_not_recorded(G, L):
    """空不许落账：None 是"正在查"，不是结论，不能写进历史。"""
    led = L.Ledger(writer="t", ttl=60)
    try:
        led.record(L.addr("没判出来"), None, "不知道", "fp|a")
    except ValueError:
        return PASS, "record(None) 抛 ValueError —— 空留在悬位上，不进历史"
    return FAIL, "None 被当成一节账写进去了：以后翻账分不清「判过」和「没判」"""


def c_per_round_ttl(G, L):
    """每节自带钟：这一节多快过期，由写它的人定。"""
    led = L.Ledger(writer="t")
    n = led.record(L.addr("一件事"), 1, "对", "fp|a", ttl=7)
    if n.get("ttl") != 7:
        return FAIL, f"record(ttl=7) 之后节上是 {n.get('ttl')!r}"
    n2 = led.record(L.addr("另一件"), 1, "对", "fp|a", ttl=3600)
    if n2.get("ttl") != 3600:
        return FAIL, "两节用了不同的钟，但账上没分开记"
    return PASS, "同一本账里两节各带各的钟（7s / 3600s）"


def c_ttl_frozen(G, L):
    """写入即固化：账本级的钟只是"从哪来"，不许事后改写历史的含意。"""
    led = L.Ledger(writer="t", ttl=60)
    k = L.addr("某判决")
    n = led.record(k, 1, "对", "fp|a")
    if n.get("ttl") != 60:
        return FAIL, f"Ledger(ttl=60) 记的账，节上 ttl={n.get('ttl')!r}（没固化）"
    led.ttl = 5
    if led.nodes[k][0]["ttl"] != 60:
        return FAIL, "改了账本级 ttl，历史节的钟跟着变了"
    return PASS, "写进去的钟不会被后来的改掉（否则等于改了对过去的解释）"


def c_pair_or_recompute(G, L):
    """配对才借：指纹对上才抄旧账，对不上必重算。"""
    led = L.Ledger(writer="t", ttl=3600)
    k = L.addr("这台车合规吗")

    led.record(k, 1, "合规", "fp|A")
    if led.probe(k, "", journal=False) is not None:
        return FAIL, "没给配对指纹也借到了 —— 账本退化成了缓存"
    if led.probe(k, "fp|B", journal=False) is not None:
        return FAIL, "指纹不同也借到了 —— 情况变了还照抄旧单"
    if led.probe(k, "fp|A", journal=False) is None:
        return FAIL, "同一个指纹反而借不到"
    return PASS, "空指纹/异指纹都不借，同指纹才借"


def c_pending_falsy(G, L):
    """悬位向保守歪：没判出来的东西，在布尔语境里必须是假。"""
    led = L.Ledger(writer="t", ttl=3600)
    k = L.addr("等探针")
    p = led.open_pending(k, why=L.EVIDENCE, need="等一次实测")
    if bool(p):
        return FAIL, "Pending 在 if 里是真值 —— 谁写 if pending 就会被骗过去"
    if led.to_dict()["stat"]["unresolved"] != 1:
        return FAIL, "悬着 1 笔，但账上说不是 1 笔"
    led.resolve(k, 1, "通了", journal=False)
    if led.to_dict()["stat"]["unresolved"] != 0:
        return FAIL, "已经收口了，账上还悬着（计数不是现数的）"
    return PASS, "bool=False；悬着=1、收口=0（现数的，不是记一辈子）"


def c_due_and_forced(G, L):
    """到点必须收口：不许永远装"正在查"。"""
    led = L.Ledger(writer="t", ttl=3600)
    k = L.addr("等不到的料")
    led.open_pending(k, why=L.EVIDENCE, need="等", decide_by=led._clock() - 1)
    if not led.due():
        return FAIL, "过了收口期限，due() 却是空的"
    led.resolve(k, None, "到点没等到料", forced=True, journal=False)
    st = led.to_dict()["stat"]
    if not st.get("forced"):
        return FAIL, "强制收口了，但账上不记 forced —— 以后分不清是判出来的还是到点认输的"
    return PASS, "到期被 due() 抓住；强制收口留痕 forced"


def c_too_fresh(G, L):
    """太新不借：同一瞬间的抖动不是结论。"""
    led = L.Ledger(writer="t", ttl=3600)
    k = L.addr("刚判完又问")
    led.record(k, 1, "对", "fp|A")
    if led.probe(k, "fp|A", min_age=10, journal=False) is not None:
        return FAIL, "刚判完就问也照借 —— 抖动被当成了结论"
    if led.why_not(k) != L.TOO_FRESH:
        return FAIL, f"没借到的原因是 {led.why_not(k)!r}，不是 TOO_FRESH（第四种空没认出来）"
    return PASS, "min_age 内不借，且原因单独成一类"


def c_antipode(G, L):
    """翻面不可配：对面是什么，不由使用者定义。"""
    if L.Ledger.antipode(0) != 1 or L.Ledger.antipode(1) != 0:
        return FAIL, "antipode 不是 1-v"
    try:
        L.Ledger.antipode(0, 1)
    except TypeError:
        return PASS, "antipode(v)=1-v，没有第二个参数可配"
    return FAIL, "antipode 能传参数 —— 翻面一旦可配，就不是翻面了"


def c_tombstone(G, L):
    """三档遗忘：prune 褪成梗概（判决、圈号留住），sweep_ttl 才立墓碑（先归档后动刀）。

    遗忘本身必须在账上看得见，否则就是偷偷改账；位置一格不许抽走，圈号一个字不许乱。
    """
    # ① prune 这把刀只降级：不立墓碑，判决和圈号留住
    led = L.Ledger(writer="t", ttl=3600)
    k = L.addr("平淡的三圈")
    for i in range(3):
        led.record(k, 0, f"第{i}圈", "fp|A")
    before = len(led.nodes[k])
    led.prune(key=k, keep_last=1)
    if len(led.nodes[k]) != before:
        return FAIL, "位置被抽走了 —— 圈号会跟着乱"
    if led.tombstones():
        return FAIL, "prune 立了墓碑 —— 墓碑只许 sweep_ttl 动"
    faded = [n for n in led.nodes[k] if n.get("demoted")]
    if len(faded) != 2:
        return FAIL, f"平淡圈没褪成梗概（褪了 {len(faded)}，应 2）"
    if any(n.get("verdict") is None for n in faded):
        return FAIL, "梗概把判决也褪没了 —— 「我判过它」这句话丢了"
    if led.by_verdict(k) is None:
        return FAIL, "翻账时读不到判决（梗概的判决要留住）"

    # ② sweep_ttl 这把刀才立墓碑，而且先归档后动刀
    with tempfile.TemporaryDirectory() as d:
        arc = os.path.join(d, "expired.jsonl")
        led2 = L.Ledger(writer="t", ttl=3600, now=lambda: 1000.0)
        for i in range(3):
            led2.record(k, 0, f"第{i}圈", "fp|A", ttl=60, ts=1000.0)
        # 不给归档位置：不扫、不立墓碑、不抛（round 45 方向 C：绝不抛契约保住）
        skipped = led2.sweep_ttl(now=99999.0)
        if skipped != 0:
            return FAIL, "不给归档位置也照扫 —— 留不下底就不写，这条丢了"
        if led2.tombstones():
            return FAIL, "底还没留就动了刀 —— 没归档不许立墓碑"
        swept = led2.sweep_ttl(now=1000.0 + 61, archive=arc)
        if swept != 3:
            return FAIL, f"过窗的节没被扫（扫了 {swept}，应 3）"
        if led2.tombstones() != 3:
            return FAIL, "扫完没立墓碑 —— 忘这一档在账上看不见"
        if led2.by_round(k) != [1, 2, 3]:
            return FAIL, "墓碑化把圈号挪了位"
        rows = open(arc, encoding="utf-8").read().splitlines()
        if len(rows) != 3:
            return FAIL, f"归档 {len(rows)} 行，应 3 行（先归档后动刀，一节一行原样）"
        if led2.sweep_ttl(now=1000.0 + 61, archive=arc) != 0:
            return FAIL, "再扫还有刀 —— 墓碑不重复扫（不幂等）"
    return PASS, f"prune 褪 {len(faded)} 节梗概（判决圈号留住）；sweep_ttl 归档 {swept} 节后立墓碑，圈号没乱"


def c_replay_consistent(G, L):
    """落盘重放一致：写出去什么，读回来就是什么。"""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "book.json")
        led = L.Ledger(writer="t", ttl=3600)
        k = L.addr("落盘对账")
        led.record(k, 1, "第一圈", "fp|A", ttl=3600)
        led.record(k, 0, "第二圈", "fp|B")
        led.save(p)
        back = L.Ledger.load(p, writer="t", ttl=3600)
        if len(back.nodes.get(k, [])) != 2:
            return FAIL, f"存了 2 节，读回 {len(back.nodes.get(k, []))} 节"
        if [n["round"] for n in back.nodes[k]] != [1, 2]:
            return FAIL, "圈号读回来乱了"
        if back.nodes[k][1]["ttl"] != 3600:
            return FAIL, "节上的钟没跟着落盘（固化在内存里没写盘）"
    return PASS, "2 节 / 圈号 1、2 / 每节的钟都在（存的与读回的对上同一份账）"


def c_save_keeps_journal(G, L):
    """KNOWN 欠账：save() 不许吃掉别的进程刚写的行。"""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "book.json")
        led = L.Ledger(writer="t", ttl=3600)
        led.record(L.addr("常驻进程"), 1, "对", "fp|A")
        led.save(p)
        short = L.Ledger(writer="t")                     # 短命进程：只 append 一行，不读全本
        short.append(p + ".jsonl", L.addr("短命进程"), 0, "另一行", "fp|B")
        led.save(p)                      # 常驻方再存一次
        back = L.Ledger.load(p, writer="t", ttl=3600)
        if L.addr("短命进程") not in back.nodes:
            return KNOWN, ("短命进程那行被 save() 吃掉了 —— 并发下会静默丢账（未修）。"
                           "这条黄是体检替你复现的欠账，不是你接错了")
    return PASS, "两个写者的账都在"


def c_borrow_is_recorded(G, L):
    """借这一下也要落账：换个进程读回来，得数得出来。"""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "book.json")
        jp = p + ".jsonl"
        led = L.Ledger(writer="t", ttl=3600)
        k = L.addr("会被借的一节")
        led.append(jp, k, 1, "对", "fp|A")
        led.save(p)

        b = L.Ledger.load(p, writer="b", ttl=3600)
        if b.probe(k, "fp|A", checked=True, journal=jp) is None:
            return FAIL, "自己的账借不出来"
        b.save(p)

        c = L.Ledger.load(p, writer="c", ttl=3600)
        hits = c.nodes[k][0]["hits"]
        if hits != 1:
            return FAIL, f"借了一次，另一个进程读回来是 {hits} 次（读-改-写会互相盖）"
        if c.nodes[k][0]["borrows"][0].get("by") != "b":
            return FAIL, "借是谁借的没留下"
    return PASS, "借这一下进流水；换个进程读回来还数得出是谁借的"


def c_replay_knows_same_node(G, L):
    """重演认得出"同一节"：不能把两圈当重复丢掉，也不能凭空造出一次翻面。"""
    led = L.Ledger(writer="t", ttl=3600)

    def node(r, ts, verdict):
        return {"key": "k", "verdict": verdict, "reason": "对", "ts": ts, "round": r,
                "value_score": None, "ctx_fp": "fp|A", "hits": 0, "last_hit": None,
                "borrows": [], "ttl": 3600, "writer": "t", "tombstone": None}

    led._replay_node("k", node(1, 100.0, 1))
    led._replay_node("k", node(2, 100.0, 1))      # 同一毫秒的另一圈
    if len(led.nodes["k"]) != 2:
        return FAIL, "同 ts 的两圈被当成了同一节 —— 重演把账吃掉一节"
    led._replay_node("k", node(1, 100.0, 1))      # 真正的重复
    if len(led.nodes["k"]) != 2:
        return FAIL, "同一节重演两次记成了两节"
    led._replay_node("k", node(2, 200.0, 0))      # 圈号被占、内容不同
    rounds = [n["round"] for n in led.nodes["k"]]
    if len(set(rounds)) != len(rounds):
        return FAIL, f"圈号重复 {rounds} —— flips() 会报出不存在的翻面"
    return PASS, "同节只记一次；同毫秒的两圈都在；圈号不重复"


def c_compact_keeps_window(G, L):
    """压实改名那一刹撞进来的行，不许丢。"""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "book.json")
        jp = p + ".jsonl"
        led = L.Ledger(writer="t", ttl=3600)
        led.append(jp, L.addr("压之前那节"), 1, "对", "fp|A")
        led.save(p)

        # 模拟"压到一半崩了"：又写一行，然后只把它改名成备份，快照没补
        late = L.Ledger(writer="t")
        late.append(jp, L.addr("窗口里撞进来的"), 0, "没写进快照", "fp|B")
        os.replace(jp, jp + ".1.compact")

        back = L.Ledger.load(p, writer="t", ttl=3600)
        if L.addr("窗口里撞进来的") not in back.nodes:
            return FAIL, "改名窗口里的行没人管 —— 静默丢账"
        if len(back.nodes[L.addr("压之前那节")]) != 1:
            return FAIL, "备份被重复吃进去（重演不幂等）"
    return PASS, "改名窗口里撞进来的行能补回来，备份不会被吃两遍"


def c_borrow_never_silent(G, L):
    """借票有地方落，或者明说不落 —— 不许悄悄借。"""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "book.json")
        jp = p + ".jsonl"
        led = L.Ledger(writer="t", ttl=3600)
        k = L.addr("会被借的一节")
        led.append(jp, k, 1, "对", "fp|A")
        led.save(p)

        b = L.Ledger.load(p, writer="t", ttl=3600)
        try:
            b.probe(k, "fp|A")
        except TypeError:
            pass
        else:
            return FAIL, "不传 journal 也借得出来 —— 借票悄悄不落账"
        try:
            b.probe(k, "fp|A", journal=None)
        except ValueError:
            pass
        else:
            return FAIL, "journal=None 也借得出来"

        c = L.Ledger.load(p, writer="t", ttl=3600)
        if c.probe(k, "fp|A", journal=False) is None:
            return FAIL, "明写 journal=False 反而借不出来"
        if c.nodes[k][0]["hits"] != 1:
            return FAIL, "journal=False 没在内存里记这一借"
        back = L.Ledger.load(p, writer="t", ttl=3600)
        if back.nodes[k][0]["hits"] != 0:
            return FAIL, "journal=False 偷偷写了流水"
    return PASS, "不传就报错；journal=False 只记内存、不写流水（明说才算）"


def c_compact_locked(G, L):
    """压实要拿锁：拿不到就让路，不许两个一起抽纸。"""
    import threading
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "book.json")
        jp = p + ".jsonl"
        lock = p + ".compact.lock"
        led = L.Ledger(writer="t", ttl=3600)
        for i in range(5):
            led.append(jp, L.addr(f"第{i}节"), 1, "对", "fp|A")
        led.save(p)

        # 要验"别人正拿着"，必须真放一个活人上去。空锁是垃圾锁（建完文件就死了），
        # 压实会等 5 秒再接管 —— 那是自愈，不是抢活人的锁。
        import subprocess
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            with open(lock, "w", encoding="utf-8") as f:
                f.write(str(holder.pid))
            got = L.Ledger.load(p, writer="t", ttl=3600).compact(p)
        finally:
            holder.kill()
            holder.wait()
        if got is not False:
            return FAIL, "锁被别人拿着还硬压 —— 两张纸一起抽"
        alive = L.Ledger.load(p, writer="t", ttl=3600)
        if len(alive.nodes) != 5:
            return FAIL, "让路那次把账动了"
        os.unlink(lock)

        # 真抢一次：一边不停追加，两个压实者同时上，一行都不许丢
        stop = {"v": False}
        def writer():
            w = L.Ledger(writer="w", ttl=3600)
            for i in range(30):
                w.append(jp, L.addr(f"抢{i}"), 1, "对", "fp|A")
            stop["v"] = True
        def compactor():
            c = L.Ledger.load(p, writer="c", ttl=3600)
            for _ in range(5):
                c.compact(p)
                c = L.Ledger.load(p, writer="c", ttl=3600)
        tw = threading.Thread(target=writer)
        tc = [threading.Thread(target=compactor) for _ in range(2)]
        tw.start(); [t.start() for t in tc]
        tw.join(); [t.join() for t in tc]
        L.Ledger.load(p, writer="c", ttl=3600).compact(p)
        end = L.Ledger.load(p, writer="c", ttl=3600)
        have = set(end.nodes)
        want = {L.addr(f"第{i}节") for i in range(5)} | {L.addr(f"抢{i}") for i in range(30)}
        if want - have:
            return FAIL, f"压实撞车丢了 {len(want - have)} 节账"
    return PASS, "锁被占就让路；两个压实者狂压 + 一直有人写，35 节一行不丢"


def c_resolve_is_recorded(G, L):
    """收口也要落账：时间逼的收口尤其要留痕，进程崩了也得看得出怎么没的。"""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "book.json")
        jp = p + ".jsonl"
        led = L.Ledger(writer="t")
        k = L.addr("到期没等到料")
        led.open_pending(k, why=L.EVIDENCE, need="补件", ctx_fp="fp|A", decide_by=0)
        led.save(p)

        b = L.Ledger.load(p, writer="t")
        try:
            b.resolve(k, forced=True)
        except TypeError:
            pass
        else:
            return FAIL, "不传 journal 也收得掉 —— 强收口悄悄不进流水"

        c = L.Ledger.load(p, writer="t")
        c.resolve(k, None, "到点没等到料", journal=jp, forced=True)
        lines = [json.loads(x) for x in open(jp, encoding="utf-8") if x.strip()]
        hits = [x for x in lines if x.get("t") == "resolve"]
        if not hits:
            return FAIL, "收口没进流水"
        if not hits[-1].get("forced"):
            return FAIL, "强收口没留 forced 的痕"
        if hits[-1].get("by") in (None, "", "unknown"):
            return FAIL, "收口没署名"
    return PASS, "不传就报错；强收口那一行带 forced 和署名"


def c_compact_says_what_happened(G, L):
    """让路 / 没活 / 压成了，三件事要分得清；活锁不许抢。"""
    with tempfile.TemporaryDirectory() as d:
        def book(name):
            p = os.path.join(d, name + ".json")
            led = L.Ledger(writer="t", ttl=3600)
            led.append(p + ".jsonl", L.addr("一节"), 1, "对", "fp|A")
            led.save(p)
            return p

        # 1) 没流水可压 → None（不是失败）
        empty = os.path.join(d, "empty.json")
        L.Ledger(writer="t", ttl=3600).save(empty)
        if L.Ledger.load(empty, writer="t", ttl=3600).compact(empty) is not None:
            return FAIL, "没有流水时没说清楚（该返回 None）"

        # 2) 压成了 → 返回备份路径，且 stat 里有数
        a_path = book("a")
        a = L.Ledger.load(a_path, writer="t", ttl=3600)
        got = a.compact(a_path)
        if not isinstance(got, str) or not got:
            return FAIL, f"压成了却没说清楚（返回 {got!r}）"
        if a.stat.get("compacted") != 1:
            return FAIL, "压成了但 stat 里没数（cron 看不出压过几次）"

        # 3) 活锁（老 mtime + 锁里写的是**真活着的外人**）→ 让路，且计数
        #    以前图省事写自己的 pid：可"我的 pid 却没握在我手里"正是"我的残渣"的样子，
        #    两件事必须用两种现场验，否则验的是一个假人。
        import subprocess
        b_path = book("b")
        lock = b_path + ".compact.lock"
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            with open(lock, "w", encoding="utf-8") as f:
                f.write(str(holder.pid))
            # mtime 新鲜 = 人家正在续命（压实每 15 秒给锁暖一下），这才是"真的活着"。
            # 不拨老 mtime：一把活锁的 mtime 绝不会老过 45 秒 —— 老成那样只可能没人握着。
            b = L.Ledger.load(b_path, writer="t", ttl=3600)
            if b.compact(b_path) is not False:
                return FAIL, "锁是活人的还抢 —— 两个压实者又同时跑了"
            if b.stat.get("compact_skipped") != 1:
                return FAIL, "让路了但 stat 里没数"
        finally:
            holder.kill()
            holder.wait()

        # 4) 死锁（主人早没了）→ 该接过来
        with open(lock, "w", encoding="utf-8") as f:
            f.write("999999")
        if L.Ledger.load(b_path, writer="t", ttl=3600).compact(b_path) is False:
            return FAIL, "锁的主人早没了还让路 —— 这本账再也不会被压实"
    return PASS, "压成/没活/让路分得清且都计数；活锁不抢、死锁能接"


def c_compact_keeps_unsaved(G, L):
    """压实以磁盘对齐，但不能把手还在手上、还没落盘的账当脏数据扔掉。"""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "book.json")
        jp = p + ".jsonl"
        L.Ledger(writer="t", ttl=3600).save(p)
        mine = L.Ledger.load(p, writer="t", ttl=3600)
        mine.record(L.addr("还没落盘的一节"), 1, reason="对", ctx_fp="fp|A")

        w = L.Ledger(writer="w", ttl=3600)
        w.append(jp, L.addr("别人写的"), 1, "对", "fp|A")
        L.Ledger.load(p, writer="w", ttl=3600).compact(p)     # 别人先压一轮
        mine.compact(p)

        back = L.Ledger.load(p, writer="t", ttl=3600)
        if L.addr("还没落盘的一节") not in back.nodes:
            return FAIL, "压实把调用方手上还没落盘的账抹掉了"
        if L.addr("别人写的") not in back.nodes:
            return FAIL, "压实把别人刚压进去的账抹掉了"
    return PASS, "对齐是合并不是覆盖：没落盘的那节和别人的那节都还在"


def c_load_does_not_fake_empty(G, L):
    """读不到就报错，不许交回一本 0 节的"好账"。（协作者 C 2026-10-31 第三轮）"""
    with tempfile.TemporaryDirectory() as d:
        ghost = os.path.join(d, "打错的路径.json")
        try:
            L.Ledger.load(ghost, writer="t", ttl=3600)
        except FileNotFoundError:
            pass
        except Exception as e:
            return FAIL, f"读不存在的账没报 FileNotFoundError，报了 {type(e).__name__}：{e}"
        else:
            return FAIL, "读一个不存在的路径，它交回一本 0 节空账 —— 路径打错和'还没写过'长得一样"

        # 显式说要空账：可以，但必须自己写出来
        try:
            led = L.Ledger.load(ghost, writer="t", ttl=3600, empty_ok=True)
        except Exception as e:
            return FAIL, f"empty_ok=True 反而报错：{type(e).__name__}：{e}"
        if led.size() != 0:
            return FAIL, "empty_ok=True 交回的不是空账"

        # 只有流水、没有快照：照旧要读得出来（短命进程的常态）
        p = os.path.join(d, "journal_only.json")
        jp = p + ".jsonl"
        w = L.Ledger(writer="w", ttl=3600)
        w.append(jp, L.addr("只有流水也读得出"), 1, "对", "fp|A")
        back = L.Ledger.load(p, writer="t", ttl=3600)
        if back.size() != 1:
            return FAIL, f"只有流水没有快照时读出 {back.size()} 节（应当 1 节）"
    return PASS, "读不到就报错，empty_ok 才给空账，只有流水仍然读得出"

def c_append_refuses_dirt(G, L):
    """参数错位不许静默写脏账 —— append 的路径和 key 各就各位。"""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "book.json")
        jp = p + ".jsonl"
        led = L.Ledger(writer="t", ttl=3600)
        k = L.addr("要被记的一节")

        # ① 少写一位路径：地址被当文件名 → 以前写出一条 key=null 的垃圾，还不报错
        try:
            led.append(k, None, "缺料", "fp|A")
        except ValueError:
            pass
        else:
            return FAIL, "把地址当路径也照写 —— 脏账静默落进一本叫 32d06a… 的文件里"
        if [f for f in os.listdir(d) if f != "book.json"]:
            return FAIL, f"错位调用留下了文件：{os.listdir(d)}"

        # ② key 空 / 不是摘要
        for bad in ("", None, 123):
            try:
                led.append(jp, bad, 1, "对", "fp|A")
            except ValueError:
                pass
            else:
                return FAIL, f"key={bad!r} 也落账了"

        # ③ 判决是字符串（"对"/"错" 不是判决）
        try:
            led.append(jp, k, "对", "对", "fp|A")
        except ValueError:
            pass
        else:
            return FAIL, "verdict='对' 也落账了 —— 判决只能 1/0/None"

        # ④ 正确的写法必须照常通
        led.append(jp, k, 1, "对", "fp|A")
        if not os.path.exists(jp):
            return FAIL, "正确写法反而没写进流水"
        back = L.Ledger.load(p, writer="t", ttl=3600)
        if k not in back.nodes:
            return FAIL, "正确写法写了但重演读不回来"
    return PASS, "错位的四种写法当场报错，正确写法照常落账"


def c_record_refuses_dirt(G, L):
    """记账口和落盘口共用一把尺：'对' / 2 / -1 / True 一律不许记。"""
    led = L.Ledger(writer="t", ttl=None)
    for bad in ("对", "0", 2, -1, 1.0, True, False, [1]):
        try:
            led.record("k1", bad, "理由", "fp|A")
        except ValueError:
            pass
        else:
            return FAIL, f"record 收下了 {bad!r} 当判决 —— 账上判决栏会写字，配对面随后就崩"
    if "k1" in led.nodes:
        return FAIL, "拒绝的时候还是留下了一节"
    led.record("k1", 1, "放行", "fp|A")
    led.record("k1", 0, "拦下", "fp|A")
    if [n["verdict"] for n in led.nodes["k1"]] != [1, 0]:
        return FAIL, "正确的 1/0 反而记不进去"
    return PASS, "记账口只认 1 和 0（整数），拒绝时不留下半节"


def c_admission_gate(G, L):
    """该不该记的闸在源头：判据说不留的，不进链、不写流水、不塌缩悬位。

    判据本身在 admission.py（双螺旋的另一条链，这里不验它算得对不对，
    只验账本这半边**有没有把口子留着、拦不拦得住**）。
    """
    never = lambda node: False
    with tempfile.TemporaryDirectory() as d:
        jp = os.path.join(d, "gate.jsonl")
        led = L.Ledger(writer="t", ttl=3600, admit=never)
        # ① 不传 = 不拦（零默认值的另一半：不给判据就不替你决定）
        plain = L.Ledger(writer="t", ttl=3600)
        if plain.record("k0", 1, "记", "fp|A") is None:
            return FAIL, "没给判据却被拦了 —— 不传 admit 必须等于不拦"
        # ② 拦下不进链，且地址不虚高（先建链再拦，keys() 会凭空多一个 0 节的地址）
        if led.record("k1", 1, "算出来的", "fp|A") is not None:
            return FAIL, "判据说不记，记账口还是返回了节点"
        if led.keys() or led.size():
            return FAIL, f"拦下了却占了地址：keys={led.keys()} size={led.size()}"
        if led.stat.get("admitted_out") != 1:
            return FAIL, "拦下没计数，账面上看不出来记账口拦过"
        # ③ 拦下不写流水 —— 写了重演会把它带回链上，闸就白装了
        if led.append(jp, "k2", 1, "算出来的", "fp|B") is not None:
            return FAIL, "append 没理会判据"
        if os.path.exists(jp):
            return FAIL, "判据说不记，流水却落了盘 —— 重演会把它带回链上"
        # ④ 收口/订正不过闸：拦掉就是抹账
        led.open_pending("k3", need="料", writer="t")
        led.resolve("k3", 1, "收了", journal=False, writer="t")
        if not led.nodes.get("k3"):
            return FAIL, "收口的判决被闸拦了 —— 欠账销了、判决没落"
        led.record("k4", 1, "原判", "fp|A")           # 先记一节（此刻 admit=never，得换开）
        led.admit = None
        led.record("k4", 1, "再记一节", "fp|A")
        src = led.nodes["k4"][0]["round"]
        led.admit = never
        if led.revisit("k4", src, 0, "判据变了") is None:
            return FAIL, "订正被闸拦了 —— 翻面从此分不出是情况变了还是判据变了"
        # ⑤ 悬位不被闸塌缩：拦的是这一节判决，不是那笔欠账
        led.open_pending("k5", need="料", writer="t")
        led.record("k5", 1, "算出来的", "fp|C")
        if "k5" not in led.open:
            return FAIL, "闸拦下判决的同时把悬位塌缩了，这笔欠账没了着落"
    return PASS, "闸在记账口源头：拦下的不进链不写流水，收口/订正/重演不过闸，悬位不塌缩"


def c_append_keeps_others_lines(G, L):
    """写自己的一节，不许把别人已提交、我还没读的行标成"已消费"。"""
    with tempfile.TemporaryDirectory() as d:
        snap = os.path.join(d, "book.json")
        jp = snap + ".jsonl"
        a = L.Ledger(writer="A", ttl=None)
        a.append(jp, "kA", 1, "第一节", "fp|A")

        c = L.Ledger.load(snap, writer="C", ttl=None)
        b = L.Ledger(writer="B", ttl=None)
        b.append(jp, "kB", 0, "别人的一节", "fp|A")   # C 还没读到这一行

        c.append(jp, "kC", 1, "我的一节", "fp|A")
        c.save(snap)

        back = L.Ledger.load(snap, writer="R", ttl=None)
        if back.by_verdict("kB") != 0:
            return FAIL, "别人那节被自己的读位置顶掉了 —— 这笔账永久读不到"
        if len(back.nodes.get("kA", [])) != 1 or len(back.nodes.get("kC", [])) != 1:
            return FAIL, "重演把自己或别人的行记了两遍"
    return PASS, "自己写入不替别人签收；重演按内容认人，不重复记账"


def c_check_lands_on_disk(G, L):
    """判出来的判决必须落盘：短命进程重启后不许重算，也不许丢。"""
    with tempfile.TemporaryDirectory() as d:
        jp = os.path.join(d, "gate.jsonl")
        calls = {"n": 0}

        def proc(content):
            calls["n"] += 1
            return (1, "放行")

        led = L.Ledger(writer="w", ttl=3600)
        gate = L.LedgerGanglion(ledger=led, journal=jp, why_if_unknown="缺料")
        if gate.check("这一节能放行吗", proc, ctx_fp="fp|v1")[0] != 1:
            return FAIL, "第一次判决就不对"
        fresh = L.Ledger(writer="r", ttl=3600)
        fresh.replay(jp)
        key = list(fresh.nodes)
        if not key or fresh.by_verdict(key[0]) != 1:
            return FAIL, "判决没落盘 —— 重启之后这一节就没了"
        gate2 = L.LedgerGanglion(ledger=fresh, journal=jp, why_if_unknown=L.EVIDENCE)
        gate2.check("这一节能放行吗", proc, ctx_fp="fp|v1", checked=True)
        if calls["n"] != 1:
            return FAIL, "已落盘的判决被重算了一遍（processor 被叫了两次）"

        # 判不了的时候开的悬位，同样要落盘 —— 不然下一个进程进来就当没这回事
        gate2.check("这一节还判不了吗", lambda c: (None, "等报价"), ctx_fp="fp|v1")
        again = L.Ledger(writer="r", ttl=3600)
        again.replay(jp)
        if not again.open:
            return FAIL, "悬位只在内存里 —— 换了进程，这笔\"还没收到料\"当场失忆"
    return PASS, "判决与悬位都随判随落盘：重启读得回来，已落账的不重算"


def c_replay_never_resurrects(G, L):
    """重演认身份不认长相：遗忘过的节不许复活，改过的节不许翻倍。"""
    with tempfile.TemporaryDirectory() as d:
        snap = os.path.join(d, "b.json")
        jp = snap + ".jsonl"
        led = L.Ledger(writer="me", ttl=None)
        for i in range(3):
            led.append(jp, "k", 1, f"第{i+1}节")
        led.prune("k", keep_last=1)
        led.save(snap)
        back = L.Ledger.load(snap, writer="r", ttl=None)
        if back.by_round("k") != [1, 2, 3]:
            return FAIL, f"遗忘过的节被重演立起来了：圈号 {back.by_round('k')}"

        snap2 = os.path.join(d, "c.json")
        jp2 = snap2 + ".jsonl"
        led2 = L.Ledger(writer="me", ttl=None)
        led2.append(jp2, "k", 1, "一节")
        led2.backfill_value("k", 1, 0.9)
        led2.save(snap2)
        back2 = L.Ledger.load(snap2, writer="r", ttl=None)
        if len(back2.nodes["k"]) != 1:
            return FAIL, "改过一节的分数之后，同一节被记成了两节"
    return PASS, "重演按身份认人：删掉的不复活，改过的不翻倍"


def c_merge_keeps_round_order(G, L):
    """合并撞号：**盘上那几节是锚**，撞进来的整批按时间顺延；链序跟着圈号走。

    两个方向都验：甲先存乙后并、乙先存甲后并。两边都必须是「盘上不动、后到的连号排后面」，
    而且链上时间不许倒流（倒流会让 flips 报没发生过的改判）。
    """
    def run(first_reasons, second_reasons, first_fp, second_fp):
        with tempfile.TemporaryDirectory() as d:
            snap = os.path.join(d, "book.json")
            jp = snap + ".jsonl"
            one = L.Ledger(writer="one", ttl=3600)
            for r in first_reasons:
                one.record("k", 1, r, first_fp)
            one.save(snap, journal=jp)
            two = L.Ledger(writer="two", ttl=3600)
            for r in second_reasons:
                two.record("k", 1, r, second_fp)
            two.save(snap, journal=jp)
            back = L.Ledger.load(snap, writer="r", ttl=3600)
            return [(n["round"], n["reason"]) for n in back.nodes["k"]], back

    got, back = run(["A-1", "A-2", "A-3"], ["B-1", "B-2"], "fp|A", "fp|B")
    if got != [(1, "A-1"), (2, "A-2"), (3, "A-3"), (4, "B-1"), (5, "B-2")]:
        return FAIL, f"甲先存、乙后并，链不对：{got}（盘上的号被动或链序错位）"

    got2, back2 = run(["B-1", "B-2"], ["A-1", "A-2", "A-3"], "fp|B", "fp|A")
    if got2 != [(1, "B-1"), (2, "B-2"), (3, "A-1"), (4, "A-2"), (5, "A-3")]:
        return FAIL, f"乙先存、甲后并，链不对：{got2}（后写者的链被切开）"
    ts = [n["ts"] for n in back2.nodes["k"]]
    if any(x > y for x, y in zip(ts, ts[1:])):
        return FAIL, "后写者的链上时间倒流（flips 会报没发生过的改判）"
    return PASS, "盘上的号不动、撞进来的整批顺延，两个方向都对"


def c_custom_journal_reaches_snapshot(G, L):
    """流水可以叫别的名字；save()/compact() 不许只认 path+".jsonl"。"""
    with tempfile.TemporaryDirectory() as d:
        snap = os.path.join(d, "book.json")
        jp = os.path.join(d, "judgments.jsonl")
        led = L.Ledger(writer="w", ttl=3600)
        gate = L.LedgerGanglion(ledger=led, journal=jp, why_if_unknown=L.EVIDENCE)
        gate.check("这句话能放行吗", lambda c: (1, "放行"), ctx_fp="fp|A")
        # 常驻那一位手上是空的：它只认 save(journal=) 能不能把短命进程写的流水吸收进来
        keeper = L.Ledger(writer="keeper", ttl=3600)
        keeper.save(snap, journal=jp)
        # 直接看快照本身：带上 journal 去读会把流水重演回来，反倒遮住"没进快照"这件事。
        with open(snap, encoding="utf-8") as f:
            snap_nodes = json.load(f).get("nodes") or {}
        if not snap_nodes:
            return FAIL, "自定义流水名里的判决没进快照 —— 照文档抄就会静默丢账"
    return PASS, "流水叫什么都行，save/load/compact 认得住"


def c_bad_line_is_isolated(G, L):
    """流水是正本，正本也要过尺；一行坏行不许把整本账关在门外。"""
    with tempfile.TemporaryDirectory() as d:
        jp = os.path.join(d, "j.jsonl")
        base = {"key": "k", "verdict": 1, "reason": "正常", "ts": 1.0, "ctx_fp": "",
                "hits": 0, "round": 1, "value_score": None, "tombstone": None,
                "last_hit": None, "borrows": []}
        with open(jp, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"t": "node", "key": "k", "node": base}, ensure_ascii=False) + "\n")
            f.write(json.dumps({"t": "node", "key": "bad",
                                "node": {"key": "bad", "verdict": "对", "ts": 2.0}},
                               ensure_ascii=False) + "\n")
            f.write(json.dumps({"t": "node", "key": "k2",
                                "node": dict(base, key="k2", reason="后面这条")},
                               ensure_ascii=False) + "\n")
        led = L.Ledger(writer="r", ttl=3600)
        try:
            led.replay(jp)
        except Exception as e:
            return FAIL, f"一条坏行让整本账读不出来：{type(e).__name__}: {e}"
        if led.by_verdict("k") != 1 or led.by_verdict("k2") != 1:
            return FAIL, "坏行前后好好的账被一起关在门外"
        if led.by_verdict("bad") is not None:
            return FAIL, "手写的坏判决顺着重演进了链"
    return PASS, "正本也过尺：坏的跳过并记账，好的照读"


def c_pending_is_not_an_error(G, L):
    """三值协议里 processor 返 Pending 是正常答案：不崩、不占格、落悬位。"""
    with tempfile.TemporaryDirectory() as d:
        jp = os.path.join(d, "j.jsonl")
        led = L.Ledger(writer="w", ttl=3600)
        gate = L.LedgerGanglion(ledger=led, journal=jp, why_if_unknown=L.EVIDENCE)
        try:
            out, _ = gate.check("还在跑", lambda c: L.Pending(L.EVIDENCE, "等第二批数据"))
        except Exception as e:
            return FAIL, f"照协议返 Pending 反而崩：{type(e).__name__}: {e}"
        if not isinstance(out, L.Pending):
            return FAIL, f"返 Pending 没被接住，变成了 {type(out).__name__}: {out!r}"
        if led.size() != 0:
            return FAIL, "还在走的那一次占了账上的一格"
        if "TypeError" in (out.need or "") or "cannot unpack" in (out.need or ""):
            return FAIL, f"把内部英文异常当成『缺什么』记下来了：{out.need}"
    return PASS, "还在走就是还在走：接得住、不占格、缺什么都记得清"


def c_garbage_locks_do_not_stop_writers(G, L):
    """一边写、一边往锁上灌垃圾（空锁 / 非数字 / 死 pid）：行不许丢，人也不许被永久堵住。

    单条锁能不能自愈有它自己的体检（「垃圾锁自己会好」）；这一条验的是**同时**发生：
    写者在写、垃圾锁在冒 —— 三种垃圾各灌一遍，三十行必须一行不少，
    而且每轮都得在十几秒内收工（不是"等 60 秒后抛"那种收工）。
    """
    import subprocess
    for rnd in range(2):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "book.json")
        jp = p + ".jsonl"
        L.Ledger(writer="seed", ttl=3600).save(p, journal=jp)
        here = os.path.dirname(os.path.abspath(G.__file__))
        worker = os.path.join(d, "w.py")
        with open(worker, "w", encoding="utf-8") as f:
            f.write("import sys, time\n"
                    "sys.path.insert(0, %r)\n"
                    "import ledger as L\n"
                    "jp, tag = sys.argv[1], sys.argv[2]\n"
                    "led = L.Ledger(writer=tag, ttl=3600)\n"
                    "for i in range(6):\n"
                    "    led.append(jp, 'k' + tag, 1, '%%s-%%d' %% (tag, i), 'fp|' + tag, writer=tag)\n"
                    "    time.sleep(0.01)\n" % here)
        workers = [subprocess.Popen([sys.executable, worker, jp, tag],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                   for tag in "abc"]
        for content in ("", "not-a-pid", "999999"):
            try:
                with open(jp + ".lock", "w", encoding="utf-8") as f:
                    f.write(content)
            except OSError:
                pass
            time.sleep(0.05)
        t0 = time.time()
        errs = []
        for w in workers:
            _, e = w.communicate(timeout=120)
            if w.returncode:
                errs.append(e.decode(errors="ignore")[:60])
        spent = time.time() - t0
        back = L.Ledger.load(p, writer="r", ttl=3600, journal=jp)
        have = {x["reason"] for v in back.nodes.values() for x in v}
        want = {"%s-%d" % (t, i) for t in "abc" for i in range(6)}
        if want - have:
            return FAIL, "灌垃圾锁时丢了 %d 行：%s" % (len(want - have), sorted(want - have)[:3])
        if errs:
            return FAIL, "写者被垃圾锁打断了：%s" % errs[0]
        if spent > 40:
            return FAIL, "灌垃圾锁这一轮耗了 %.0f 秒（该在几秒内自己好）" % spent
    return PASS, "边写边灌空锁/垃圾锁/死锁，18 行一行不少、写者一次没被打断"


def c_garbage_lock_self_heals(G, L):
    """锁要是空文件、或者 pid 看着活着其实没人握着，也必须自己好 —— 不能靠人上去删。

    两种来源都真实：
    - 建锁之后、写 pid 之前进程死了 → 0 字节锁。谁都没有 pid 可查，"主人已死"这条永远不成立。
    - pid 被系统复用给新进程（本机实测 150 个短命进程里 23% 撞号），或者进程退了句柄还没释放
      → `_alive` 报"活着"，可它根本没握着这把锁。
    以前这两种都永久堵死：写者烧满等待上限再抛，账本从此写不进去。
    """
    import subprocess
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "book.json")
        jp = p + ".jsonl"
        L.Ledger(writer="t", ttl=3600).save(p, journal=jp)

        # 1) 0 字节锁
        open(jp + ".lock", "w").close()
        t0 = time.time()
        L.Ledger(writer="t", ttl=3600).append(jp, "k1", 1, "空锁之后", "fp|A")
        dt = time.time() - t0
        if dt > 15:
            return FAIL, f"空锁把写者卡了 {dt:.0f} 秒（该在几秒内接管）"

        # 2) pid 看着活着、mtime 老得离谱 —— 真持锁者每 15 秒续命，老成这样只可能没人握着
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            lock = p + ".compact.lock"
            with open(lock, "w", encoding="utf-8") as f:
                f.write(str(holder.pid))
            old = time.time() - 3600
            os.utime(lock, (old, old))
            t0 = time.time()
            got = L.Ledger.load(p, writer="t", ttl=3600).compact(p, journal=jp)
            dt = time.time() - t0
            if not got:
                return FAIL, f"看起来活着的死锁没接管（{dt:.0f} 秒后返回 {got!r}）"
        finally:
            holder.kill()
            holder.wait()

        # 3) 空锁 + 场上没有写者：压实也得自己等到接管的钟点，不能"让一次路"就一直压不动
        #    （协作者 C第九轮实测：空锁时 compact 0.0s 返回 False、锁留在盘上，没有写者来踩就永久卡住）
        open(p + ".compact.lock", "w").close()      # 压实的门锁是空的（建完文件就死了）
        got = L.Ledger.load(p, writer="t", ttl=3600).compact(p, journal=jp)
        if not got:
            return FAIL, "没有写者来踩，空锁就把压实永久卡住了（返回 %r）" % (got,)

        back = L.Ledger.load(p, writer="r", ttl=3600, journal=jp)
        if not back.nodes.get("k1"):
            return FAIL, "锁是自愈了，行却没了"
    return PASS, "空锁几秒内接管；pid 撞号/句柄未释放的死锁靠 mtime 兜底接管"


def c_dead_lock_taken_over(G, L):
    """锁的主人已经死了：下一个人当场接管，不许陪它等 mtime 过期。

    写者 unlink 失败会把锁留在盘上（主人随后退出，pid 变死的）。
    以前接管门槛是"mtime > 30 秒"，而等锁的人只等 12 秒 ——
    残锁一出现，后面每个写者白等一轮再抛，整批判决就这么没了（协作者 B第七轮实测 2%）。
    """
    import subprocess
    d = tempfile.mkdtemp()
    p = os.path.join(d, "led.json")
    jp = p + ".jsonl"
    L.Ledger(writer="t", ttl=None).save(p, journal=jp)
    # 造一把残锁：pid 写的是一个**根本不存在的进程**（比进程刚退、句柄还没释放更干净）
    dead = 999999
    if L.Ledger._alive(dead):
        return PASS, "这台机器上 999999 号进程存在，跳过（不是规矩的问题）"
    for name in (jp + ".lock", p + ".compact.lock"):
        with open(name, "w", encoding="utf-8") as f:
            f.write(str(dead))
    t0 = time.time()
    led = L.Ledger(writer="t", ttl=None)
    led.append(jp, "k", 1, "残锁之后", "fp|A")
    dt = time.time() - t0
    if dt > 1.0:
        return FAIL, f"主人已死的锁，写者白等了 {dt:.1f} 秒"
    bak = led.compact(p, journal=jp)
    if not bak:
        return FAIL, f"残锁还在，压实让路了（返回 {bak!r}）"
    back = L.Ledger.load(p, writer="r", ttl=None, journal=jp)
    if not back.nodes.get("k"):
        return FAIL, "锁是清掉了，行却没了"
    return PASS, "主人已死的锁当场接管，写与压实都没被卡"


def c_compact_under_load_loses_nothing(G, L):
    """一边写一边压：压得越勤越不许丢行，备份名也不许互相盖。"""
    import glob as _glob
    import threading
    with tempfile.TemporaryDirectory() as d:
        snap = os.path.join(d, "book.json")
        jp = snap + ".jsonl"
        seed = L.Ledger(writer="seed", ttl=3600)
        seed.append(jp, "seed", 1, "起", "fp")
        seed.save(snap, journal=jp)

        errors = []

        def writer(i):
            led = L.Ledger(writer=f"w{i}", ttl=3600)
            for n in range(10):
                try:
                    led.append(jp, f"k{i}-{n}", 1, f"第{n}条", "fp")
                except Exception as e:
                    errors.append(repr(e)[:60])
                    return

        ths = [threading.Thread(target=writer, args=(i,)) for i in range(3)]
        for t in ths:
            t.start()
        for _ in range(5):
            L.Ledger(writer="C", ttl=3600).compact(snap, journal=jp)
        for t in ths:
            t.join()

        back = L.Ledger.load(snap, writer="r", ttl=3600)
        want = {f"k{i}-{n}" for i in range(3) for n in range(10)} | {"seed"}
        missing = sorted(want - set(back.nodes))
        if errors:
            return FAIL, f"压实时写者被打断了：{errors[0]}"
        if missing:
            return FAIL, f"一边写一边压，丢了 {len(missing)} 行：{missing[:3]}"
        baks = _glob.glob(jp + ".*.compact")
        if len(baks) != len(set(baks)):
            return FAIL, "压实备份撞名了"
    return PASS, "一边写一边压：31 行一行不丢，备份各叫各的名"


def c_merge_keeps_time_order(G, L):
    """合并之后链上的时间必须单调：不单调 → flips 会报没发生过的改判。

    反向合并（盘上 B 先判、随后 A 拿三节更晚的账存进来）最容易露出这个洞：
    只把撞号那节顺延，没撞号的那节留在原位，整条链被切成 (1,B-1)(2,B-2)(3,A-3)(4,A-1)(5,A-2)。
    """
    with tempfile.TemporaryDirectory() as d:
        snap = os.path.join(d, "book.json")
        jp = snap + ".jsonl"
        b = L.Ledger(writer="B", ttl=3600)
        b.record("k", 1, "B-1", "fp|B", writer="B")
        b.record("k", 0, "B-2", "fp|B", writer="B")
        b.save(snap, journal=jp)

        a = L.Ledger(writer="A", ttl=3600)
        for i, v in enumerate((0, 0, 1), 1):
            a.record("k", v, f"A-{i}", "fp|A", writer="A")
        a.save(snap, journal=jp)

        back = L.Ledger.load(snap, writer="r", ttl=3600)
        chain = back.nodes["k"]
        ts = [n["ts"] for n in chain]
        if any(x > y for x, y in zip(ts, ts[1:])):
            return FAIL, f"链上时间倒流：{[(n['round'], n['reason']) for n in chain]}"
        if [n["round"] for n in chain] != list(range(1, 6)):
            return FAIL, f"圈号不连续：{[n['round'] for n in chain]}"
        if chain[0]["reason"] != "B-1" or chain[1]["reason"] != "B-2":
            return FAIL, "先判的那两节被挤到后面去了"
        flips = back.flips("k")
        if len(flips) != 2:
            return FAIL, f"假翻面：报了 {len(flips)} 次，真翻面只有 2 次"
    return PASS, "反向合并后链仍按时间排，翻面数不虚增"


def c_zero_business(G, L):
    """零业务词：标准件不认车、不认店、不认币。"""
    path = L.__file__
    tree = ast.parse(open(path, encoding="utf-8").read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id.lower())
        elif isinstance(node, ast.Attribute):
            names.add(node.attr.lower())
        elif isinstance(node, ast.arg):
            names.add(node.arg.lower())
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name.lower())
    hit = sorted(n for n in names if n in BUSINESS_WORDS)
    if hit:
        return FAIL, "标识符里混进业务词：" + ", ".join(hit)
    return PASS, "ledger.py 的标识符里没有业务词（注释和示例不算）"


def c_stuck_exit(G, L):
    """出站口（三圈况，素名）：卡了多久问得出、信号落得下、不该响的不响。"""
    with tempfile.TemporaryDirectory() as d:
        jp = os.path.join(d, "signal.jsonl")
        led = L.Ledger(writer="t", ttl=3600, now=lambda: 110.0)
        k = L.addr("悬着的那条")
        led.open_pending(k, why=L.EVIDENCE, need="差料", now=100.0)
        led.open_pending(k, why=L.EVIDENCE, need="差料", now=105.0)
        if led.stuck(key=k):
            return FAIL, "问满 2 圈就报卡壳 —— 停滞阈值被默认掉了（默认要 3 圈）"
        led.open_pending(k, why=L.EVIDENCE, need="差料", now=108.0)
        ev = led.stuck(key=k)
        if len(ev) != 1 or ev[0].get("type") != "stuck":
            return FAIL, f"转满 3 圈没报卡壳：{ev}"
        if abs(ev[0].get("idle", -1) - 10.0) > 1e-6:
            return FAIL, f"「卡了多久没动」没算出来：idle={ev[0].get('idle')!r}，应是 10 秒"
        # 三圈况同框：翻了 / 卡了 / 通了各出一条
        k2 = L.addr("翻过面的那条")
        led.record(k2, 1, "先判是", "fp|A", ts=100.0)
        led.record(k2, 0, "再判否", "fp|B", ts=105.0)
        if led.probe(k2, "fp|B", checked=True, journal=False) is None:
            return FAIL, "自检失败：这一节借不出来，「通了」那一路没法验"
        k3 = L.addr("既不翻也不卡的那条")          # 不该响的：只判过一次、没人翻、不悬
        led.record(k3, 1, "判过一次", "fp|C", ts=109.0)
        got = led.signals(journal=jp)
        types = sorted(e["type"] for e in got)
        if types != ["converged", "flip", "stuck"]:
            return FAIL, f"三型信号不齐（该响的没响 / 不该响的响了）：{types}"
        # 出站口先落日志，且落账要署名
        with open(jp, encoding="utf-8") as f:
            lines = [json.loads(x) for x in f.read().splitlines() if x.strip()]
        if len(lines) != 3 or any(l.get("t") != "signal" or l.get("by") != "t" for l in lines):
            return FAIL, f"信号没落成日志，或落了没署名：{lines}"
        # 信号是日志不是账：换个人读回来只数响过几回，一条判决都不许多
        fresh = L.Ledger(writer="t", ttl=3600)
        fresh.replay(jp)
        if fresh.stat.get("signals") != 3 or fresh.nodes:
            return FAIL, f"信号行混进了账（节点 {len(fresh.nodes)} 条）或没数出来：{fresh.stat}"
        # 不给 journal 当场报错：默认不落账 = 教人踩「响过没人知道」那个坑
        try:
            led.signals(journal=None)
        except ValueError:
            pass
        else:
            return FAIL, "不给 journal 也把信号发出去了 —— 出站口响过、没人知道"
        # 没署名不许落账
        try:
            L.Ledger(now=lambda: 110.0).signals(journal=jp)
        except ValueError:
            return PASS, "卡了多久（idle 10s）问得出；翻/卡/通各 1 条、不该响的不响；信号落日志带署名、不进链；不给 journal / 不署名当场报错"
        return FAIL, "没署名也把信号写出去了 —— 出事时谁都能说不是我"


def c_stuck_verdict(G, L):
    """停滞第二档（素名）：同 key 转满 N 圈、判决一直不更新 —— 报事实，不替谁下结论。"""
    with tempfile.TemporaryDirectory() as d:
        jp = os.path.join(d, "stale.jsonl")
        led = L.Ledger(writer="t", ttl=3600, now=lambda: 110.0)
        k = L.addr("三圈一个样")
        for ts in (100.0, 105.0, 108.0):
            led.record(k, 1, "又判了一遍", "fp|A", ts=ts)
        ev = led.stuck(key=k)
        if len(ev) != 1 or ev[0].get("kind") != "判决不更新":
            return FAIL, f"转满 3 圈判决一个样没报第二档：{ev}"
        if ev[0].get("rounds") != 3 or ev[0].get("verdict") != 1:
            return FAIL, f"第二档报的圈数/判决不对：{ev[0]}"
        if abs(ev[0].get("idle", -1) - 10.0) > 1e-6:
            return FAIL, f"「判决多久没更新」没算出来：idle={ev[0].get('idle')!r}，应是 10 秒"
        # 判决更新过 = 有过进展：翻过面的不许当停滞报
        k2 = L.addr("翻过面的那条")
        for ts, v in ((100.0, 1), (105.0, 0), (108.0, 0)):
            led.record(k2, v, "判了一遍", "fp|B", ts=ts)
        if led.stuck(key=k2):
            return FAIL, f"翻过面的也当第二档报了（更新过就是有过进展）：{led.stuck(key=k2)}"
        # 没转满圈的不许报（阈值是空格，但默认 3 圈不许被默认掉）
        k3 = L.addr("两圈一个样")
        led.record(k3, 0, "判", "fp|C", ts=100.0)
        led.record(k3, 0, "再判", "fp|C", ts=105.0)
        if led.stuck(key=k3):
            return FAIL, "问满 2 圈就报第二档 —— 停滞阈值被默认掉了（默认要 3 圈）"
        if led.stuck(key=k, threshold=4):
            return FAIL, "threshold=4 也报了 —— 阈值不是空格吗，怎么不听调用了"
        # 两档不重不漏：第一档报过的地址不重报
        led.open_pending(k, why=L.EVIDENCE, need="差料", now=109.0)
        for ts in (109.0, 110.0):
            led.open_pending(k, why=L.EVIDENCE, need="差料", now=ts)
        both = led.stuck(key=k)
        if len(both) != 1 or both[0].get("kind") != "悬位没塌缩":
            return FAIL, f"同一条地址两档各报一条（该只报第一档）：{both}"
        # 出站口照旧：第二档落日志、带署名、不进链
        led2 = L.Ledger(writer="t", ttl=3600, now=lambda: 110.0)
        for ts in (100.0, 105.0, 108.0):
            led2.record(k, 1, "又判了一遍", "fp|A", ts=ts)
        got = [e for e in led2.signals(journal=jp) if e.get("kind") == "判决不更新"]
        if len(got) != 1:
            return FAIL, f"第二档没走出站口（signals 里查不到）：{got}"
        with open(jp, encoding="utf-8") as f:
            lines = [json.loads(x) for x in f.read().splitlines() if x.strip()]
        mine = [l for l in lines if l.get("kind") == "判决不更新"]
        if len(mine) != 1 or mine[0].get("t") != "signal" or mine[0].get("by") != "t":
            return FAIL, f"第二档信号没落成带署名的日志：{mine}"
        fresh = L.Ledger(writer="t", ttl=3600)
        fresh.replay(jp)
        if fresh.stat.get("signals") != len(lines) or fresh.nodes:
            return FAIL, f"第二档信号混进了账：{fresh.stat} / 节 {len(fresh.nodes)}"
        return PASS, "第二档报得出（3 圈一个样、idle 10s）、翻过面与没转满的不响、阈值是空格、两档不重报、信号落日志带署名不进链"


def c_two_sides(G, L):
    """结算两行（素名：预计 / 实际）：两笔都留痕、差值回流、缺笔不是 0 分。"""
    with tempfile.TemporaryDirectory() as d:
        jp = os.path.join(d, "two.jsonl")
        p = os.path.join(d, "book.json")
        led = L.Ledger(writer="t", ttl=3600, now=lambda: 200.0)
        k = L.addr("预计与实际")
        led.expect(k, 0.7, journal=jp)
        led.actual(k, 0.5, journal=jp)
        if abs((led.gap(k) if led.gap(k) is not None else -1) - 0.2) > 1e-9:
            return FAIL, f"差值没回流：gap={led.gap(k)!r}（应是 0.7 − 0.5 = 0.2）"
        k2 = L.addr("只有预计")
        led.expect(k2, 0.9, journal=jp)
        if led.gap(k2) is not None:
            return FAIL, f"缺笔被当成了 0 分：gap={led.gap(k2)!r} —— 还没结算 ≠ 正好猜中"
        try:
            led.expect(L.addr("把话当数"), "大概", journal=jp)
        except ValueError:
            pass
        else:
            return FAIL, "非数字也收了：差值算不出来，账上留的就是一句谎"
        try:
            led.actual(k, 0.4, journal=None)
        except ValueError:
            pass
        else:
            return FAIL, "不给 journal 也把笔写下去了 —— 换个人读回来就少一笔"
        # 流水是正本：换一本账只重演流水，差值也算得出来
        fresh = L.Ledger(writer="t", ttl=3600)
        fresh.replay(jp)
        if fresh.gap(k) is None or abs(fresh.gap(k) - 0.2) > 1e-9:
            return FAIL, f"换个人读流水，差值算不出来：gap={fresh.gap(k)!r}"
        if fresh.gap(k2) is not None or len(fresh.nodes):
            return FAIL, "缺笔被补成了数 / 两行混进了判决链"
        # 快照也带这两行：落盘读回来不丢
        led.save(p)
        back = L.Ledger.load(p, ttl=3600)
        if back.gap(k) is None or abs(back.gap(k) - 0.2) > 1e-9 or back.gap(k2) is not None:
            return FAIL, f"落盘读回来丢了两行：gap={back.gap(k)!r} / {back.gap(k2)!r}"
        with open(jp, encoding="utf-8") as f:
            seen = {json.loads(x).get("t") for x in f.read().splitlines() if x.strip()}
        if not {"expect", "actual"} <= seen:
            return FAIL, f"两行没落进流水（正本里查不到）：{sorted(seen)}"
        return PASS, "预计/实际两行都留痕、差值 0.2 回流、缺笔报 None、非数字与缺 journal 当场拒；流水与快照都读得回来"


def c_signal_boundary(G, L):
    """信号 vs 期望收益闸：信号只报事实，叫不叫醒归外面那道闸（≤0 不记不叫）。

    两块的边界钉死，防止后人把信号直接接成调用：
      ① 事件里没有决策字段（从事件里读不出「该叫了」），落日志不进链、不改判决、
         不动悬位、不改别的统计；
      ② 判据是双螺旋的另一条链 —— admission 不 import 账本、账本不 import 判据，
         且式子本身「期望收益 ≤ 0 → DROP（不记不叫）」钉在判据那半边。
    """
    def _imports(path):
        mods = set()
        for node in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
            if isinstance(node, ast.Import):
                mods.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module.split(".")[0])
        return mods

    with tempfile.TemporaryDirectory() as d:
        jp = os.path.join(d, "signal.jsonl")
        led = L.Ledger(writer="t", ttl=3600, now=lambda: 200.0)
        k1 = L.addr("悬着的那条")
        for t in (100.0, 105.0, 108.0):                 # 造第一档：问满 3 圈
            led.open_pending(k1, why=L.EVIDENCE, need="差料", now=t)
        k2 = L.addr("翻过面的那条")                      # 造 flip + converged
        led.record(k2, 1, "先判是", "fp|A", ts=100.0)
        led.record(k2, 0, "再判否", "fp|B", ts=105.0)
        led.probe(k2, "fp|B", checked=True, journal=False)
        # 账面快照：信号响完，判决链 / 悬位 / 其余统计一个字都不许变
        chain_before = {k: [n["verdict"] for n in v] for k, v in led.nodes.items()}
        open_before = {k: p.tries for k, p in led.open.items()}
        stat_before = {k: v for k, v in led.stat.items() if k != "signals"}
        ev = led.signals(journal=jp, now=200.0)
        if not ev:
            return FAIL, "三型信号一条都没响，边界这道体检等于没跑"
        # ① 事件里没有决策字段：信号是日志，不是调用指令
        banned = {"action", "gain", "admit", "wake", "should", "call", "notify", "next"}
        hits = sorted({f for e in ev for f in e} & banned)
        if hits:
            return FAIL, f"信号事件里长出决策字段 {hits} —— 事件能被直接读成「该叫了」"
        if {k: [n["verdict"] for n in v] for k, v in led.nodes.items()} != chain_before:
            return FAIL, "信号改了判决链 —— 日志混成账了"
        if {k: p.tries for k, p in led.open.items()} != open_before:
            return FAIL, "信号动了悬位 —— 起念不该替谁塌缩欠账"
        stat_after = {k: v for k, v in led.stat.items() if k != "signals"}
        if stat_after != stat_before:
            return FAIL, f"信号改了别的统计：{stat_before} → {stat_after}"
        # ② 双螺旋：两条链互不 import（谁写错，另一侧不该跟着错）
        base = os.path.dirname(os.path.abspath(G.__file__))
        if "ledger" in _imports(os.path.join(base, "admission.py")):
            return FAIL, "admission.py 把账本拉进来了 —— 判据和记账成了一条链"
        if "admission" in _imports(os.path.join(base, "ledger.py")):
            return FAIL, "ledger.py 把判据拉进来了 —— 账本开始替判据算数"
        # ③ 式子本身：期望收益 = 重放概率 × 重算成本 − 写入成本，≤0 不记不叫
        path = os.path.join(base, "admission.py")
        if not os.path.exists(path):
            return FAIL, "admission.py 不见了 —— 「叫不叫醒」那把尺没地方放"
        spec = importlib.util.spec_from_file_location("_conf_admission_boundary", path)
        adm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adm)
        if adm.decide(0.0, 1000.0)[0] != "DROP" or adm.decide(0.001, 100.0, 1.0)[0] != "DROP":
            return FAIL, "期望收益 ≤ 0 却 STORE 了 —— 不值当的也记/也叫"
        if adm.decide(0.5, 100.0)[0] != "STORE":
            return FAIL, "期望收益 > 0 却 DROP 了 —— 判据把该记的拦了"
        return PASS, "信号无决策字段、不改链不塌悬位不动别的统计；两条链互不 import；期望收益 ≤0 → DROP（不记不叫）、>0 → STORE"


# ══════════════ README 示例可跑性：抽一段真跑（round 51 加） ══════════════
# 立场：测试问"功能对不对"，体检问"规矩还在不在" —— 而文档里的示例跑不通，
# 是第三种坏：规矩写在纸上、纸自己是假的。抽不到可跑的东西就 SKIP，不许冒充绿。

_FENCE = re.compile(r"```(?:python|py)[ \t]*\r?\n(.*?)```", re.S)
# 抽到这些就算占位：文档明写"要换成真路径/真前置"，本来就跑不通，不算示例坏了
_SNIPPET_PLACEHOLDER = ("D:/repo", "<路径>", "C:/some", "换真路径", "…")


def readme_python_blocks(text):
    """README 里 ```python 代码块的正文，按出现顺序返回。"""
    return [m.strip("\r\n") for m in _FENCE.findall(text)]


def run_snippet(code, repo, timeout=60):
    """把一段示例丢进**干净目录**真跑：import 得到仓库里的模块，碰不到仓库的文件。

    示例里写文件是允许的 —— cwd 是临时目录，跑完随目录一起消失。
    返回 (过没过, 一句话)。
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = repo + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONIOENCODING"] = "utf-8"              # 中文 print 不许因为编码炸掉
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "snippet.py")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(code + "\n")
        try:
            p = subprocess.run([sys.executable, path], cwd=td, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, f"超过 {timeout} 秒没跑完（示例在等人，或者在死循环）"
        out = (p.stdout or b"").decode("utf-8", "replace")
        if p.returncode == 0:
            return True, f"rc=0，输出 {len(out.splitlines())} 行"
        tail = " ⏎ ".join(x.strip() for x in out.strip().splitlines()[-2:])[:160]
        return False, f"rc={p.returncode}：{tail or '一点输出都没有'}"


def c_readme_snippet(G, L):
    """README 的代码块抽一段真跑：文档里的示例必须一直是能跑的。

    抽不到（没有 README / 没有 python 块 / 全是占位）→ SKIP：
    **没东西可验就是没验**，不许把"没验"算成"绿"—— 自检段自己写死的那句话。
    """
    repo = os.path.dirname(os.path.abspath(G.__file__))
    path = os.path.join(repo, "README.md")
    if not os.path.exists(path):
        return SKIP, "抽不到：这份拷贝没有 README.md"
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        return SKIP, f"抽不到：README.md 读不出来（{type(e).__name__}）"
    blocks = readme_python_blocks(text)
    cand = [b for b in blocks if b and not any(t in b for t in _SNIPPET_PLACEHOLDER)]
    if not cand:
        return SKIP, f"抽不到：README 里 {len(blocks)} 个 python 块不是空的就是占位"
    ok, detail = run_snippet(cand[0], repo)
    head = len(cand[0].splitlines())
    if ok:
        return PASS, f"第一个可抽的 python 块（{head} 行）真跑通过 · {detail}"
    return FAIL, f"README 示例跑不通（第一个可抽的块 {head} 行）· {detail}"


CHECKS = [
    ("骨节本体未退化", c_v1_bone),
    ("零默认值", c_no_defaults),
    ("谁写的要署名", c_writer_signed),
    ("只记不借要吵", c_record_only),
    ("空不许落账", c_empty_not_recorded),
    ("每节自带钟", c_per_round_ttl),
    ("写入即固化", c_ttl_frozen),
    ("配对才借", c_pair_or_recompute),
    ("悬位向保守歪", c_pending_falsy),
    ("到点必须收口", c_due_and_forced),
    ("太新不借", c_too_fresh),
    ("翻面不可配", c_antipode),
    ("三档遗忘留痕", c_tombstone),
    ("落盘重放一致", c_replay_consistent),
    ("落盘不吃别人的流水", c_save_keeps_journal),
    ("借了要落账", c_borrow_is_recorded),
    ("重演认得出同一节", c_replay_knows_same_node),
    ("压实不丢改名窗口", c_compact_keeps_window),
    ("借票不许悄悄不落账", c_borrow_never_silent),
    ("记错位置的账当场报错", c_append_refuses_dirt),
    ("读不到别假装是空账", c_load_does_not_fake_empty),
    ("记账口也守一把尺", c_record_refuses_dirt),
    ("该不该记在源头拦", c_admission_gate),
    ("写自己不替别人签收", c_append_keeps_others_lines),
    ("判出来的必须落盘", c_check_lands_on_disk),
    ("重演不复活旧账", c_replay_never_resurrects),
    ("合并后链序跟圈号", c_merge_keeps_round_order),
    ("流水叫什么都认", c_custom_journal_reaches_snapshot),
    ("坏行不锁死整本", c_bad_line_is_isolated),
    ("还在走不是异常", c_pending_is_not_an_error),
    ("边写边压不丢行", c_compact_under_load_loses_nothing),
    ("死锁当场接管", c_dead_lock_taken_over),
    ("垃圾锁自己会好", c_garbage_lock_self_heals),
    ("灌垃圾锁不丢行", c_garbage_locks_do_not_stop_writers),
    ("合并后链上时间单调", c_merge_keeps_time_order),
    ("压实要拿锁", c_compact_locked),
    ("收口也要落账", c_resolve_is_recorded),
    ("压实说得清让没让路", c_compact_says_what_happened),
    ("压实不抹手上的账", c_compact_keeps_unsaved),
    ("零业务词", c_zero_business),
    ("出站口落账", c_stuck_exit),
    ("停滞第二档", c_stuck_verdict),
    ("预计实际两行", c_two_sides),
    ("信号不越权", c_signal_boundary),
    ("README 示例真跑", c_readme_snippet),
]


def run(repo, as_json=False):
    G, L = _load(repo)
    results = []
    for name, fn in CHECKS:
        try:
            status, detail = fn(G, L)
        except Exception as e:                       # 体检脚本自己不许把人家验收搞崩
            status, detail = FAIL, f"{type(e).__name__}: {e}"
        results.append({"check": name, "status": status, "detail": detail})

    if as_json:
        print(json.dumps({"repo": repo, "results": results}, ensure_ascii=False, indent=2))
    else:
        bad = [r for r in results if r["status"] == FAIL]
        known = [r for r in results if r["status"] == KNOWN]
        skip = [r for r in results if r["status"] == SKIP]
        print(f"标准件体检 · {repo}")
        print("─" * 64)
        for r in results:
            mark = {"PASS": "✅", "FAIL": "❌", "KNOWN": "⚠️ ", "SKIP": "⏭️ "}[r["status"]]
            print(f"{mark} {r['check']:<12} {r['detail']}")
        print("─" * 64)
        line = (f"绿 {len(results)-len(bad)-len(known)-len(skip)}/{len(results)}"
                f"  ·  红 {len(bad)}  ·  已知欠账 {len(known)}")
        if skip:                       # 跳过不是绿也不是红：没验就是没验，单独报
            line += f"  ·  跳过 {len(skip)}"
        print(line)
        if bad:
            print("\n红不是「不通用」，是「这一条规矩还没落到代码上」。")
        if skip:
            print("⏭️ 跳过 = 这一项抽不到可验的东西，**没验**；别把它读成通过。")
    return 0 if not [r for r in results if r["status"] == FAIL] else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="判决账本标准件体检")
    ap.add_argument("repo", nargs="?", default=".", help="账本所在目录（含 ganglion.py + ledger.py）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    sys.exit(run(os.path.abspath(a.repo), a.json))

# -*- coding: utf-8 -*-
"""2026-10-31 两份外部评审（协作者 B / 协作者 C）挖出的六个真坑，逐条钉住。

每一处在修之前都应该红，修完后必须绿；谁顺手改回去，这里当场拦住。

① record() 判决零校验：'对' / 2 / -1 / True 全收 → 账上写 '对'，antipode 崩
② append() 把读位置顶到文件末尾 → 别人已提交的行被标成"已消费"，永久读不到
③ check() 判出来的判决只进内存、不落流水 → 短命进程重启后判决全丢
④ resolve() 唯一一笔悬位收口时 had 记成 null → "收口了几笔"永远漏一类
⑤ six_port() 六个面取自两三节（①最旧有分的 / ②③⑤最新 / ④全串）→ 拼不回一节
⑥ 合并撞号时重排圈号 → revisit_of / borrows 指错节，prune 忘错节（静默）
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from ledger import Ledger, LedgerGanglion, EVIDENCE  # noqa: E402


# ── ① 判决只认 1 / 0 ────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["对", "0", "true", 2, -1, 1.0, True, False, [1], {"v": 1}])
def test_record_refuses_anything_but_one_and_zero(bad):
    led = Ledger(writer="t", ttl=None)
    with pytest.raises(ValueError):
        led.record("k1", bad, "理由", "fp|A")
    assert "k1" not in led.nodes          # 拒绝的时候不许留下一节


def test_record_accepts_one_and_zero():
    led = Ledger(writer="t", ttl=None)
    led.record("k1", 1, "放行", "fp|A")
    led.record("k1", 0, "拦下", "fp|A")
    assert [n["verdict"] for n in led.nodes["k1"]] == [1, 0]


# ── ② 别人的行不许被自己顶掉 ──────────────────────────────────────────

def test_append_does_not_swallow_another_process_lines(tmp_path):
    snap = str(tmp_path / "book.json")
    jp = snap + ".jsonl"

    a = Ledger(writer="A", ttl=None)
    a.append(jp, "kA", 1, "第一节", "fp|A")

    # C 先读了这本账（读位置停在 kA 之后）
    c = Ledger.load(snap, writer="C", ttl=None)
    assert c.by_verdict("kA") == 1

    # 别人在这之后、C 写之前提交了一节
    b = Ledger(writer="B", ttl=None)
    b.append(jp, "kB", 0, "别人的一节", "fp|A")

    # C 自己再记一节并存盘
    c.append(jp, "kC", 1, "我的一节", "fp|A")
    c.save(snap)

    again = Ledger.load(snap, writer="R", ttl=None)
    assert again.by_verdict("kB") == 0, "别人那节被自己的读位置顶掉了"
    assert again.by_verdict("kA") == 1
    assert again.by_verdict("kC") == 1


def test_append_does_not_duplicate_its_own_line_on_replay(tmp_path):
    snap = str(tmp_path / "book.json")
    jp = snap + ".jsonl"
    a = Ledger(writer="A", ttl=None)
    a.append(jp, "kA", 1, "第一节", "fp|A")
    a.save(snap)
    again = Ledger.load(snap, writer="R", ttl=None)
    assert len(again.nodes["kA"]) == 1, "自己那行被重演记了两遍"


# ── ③ check() 的判决必须落盘 ─────────────────────────────────────────

def test_check_writes_judgement_to_journal(tmp_path):
    jp = str(tmp_path / "gate.jsonl")
    calls = {"n": 0}

    def proc(content):
        calls["n"] += 1
        return (1, "放行")

    led = Ledger(writer="w", ttl=3600)
    gate = LedgerGanglion(ledger=led, journal=jp, why_if_unknown="缺料")
    v1, _ = gate.check("这句话能放行吗", proc, ctx_fp="fp|v1")
    assert v1 == 1 and calls["n"] == 1

    # 短命进程重启：判决必须还在，processor 不许被再调一次
    fresh = Ledger(writer="r", ttl=3600)
    fresh.replay(jp)
    key = list(fresh.nodes)[0]
    assert fresh.by_verdict(key) == 1, "判决没落盘：重启就丢了"
    gate2 = LedgerGanglion(ledger=fresh, journal=jp, why_if_unknown="缺料")
    v2, why = gate2.check("这句话能放行吗", proc, ctx_fp="fp|v1", checked=True)
    assert v2 == 1 and calls["n"] == 1, "已落账的判决被重算了一遍"
    assert "借账" in why


def test_check_persists_pending_on_disk(tmp_path):
    jp = str(tmp_path / "gate.jsonl")
    led = Ledger(writer="w", ttl=3600)
    gate = LedgerGanglion(ledger=led, journal=jp, why_if_unknown=EVIDENCE)
    gate.check("这句还判不了吗", lambda c: (None, "等报价"), ctx_fp="fp|v1")

    fresh = Ledger(writer="r", ttl=3600)
    fresh.replay(jp)
    assert fresh.open, "悬位没落盘：换一个进程就当没这回事，tries 从头数"


# ── ④ 收口那一笔，had 要说实话 ───────────────────────────────────────

def test_resolve_records_had_true_for_the_only_pending(tmp_path):
    jp = str(tmp_path / "j.jsonl")
    led = Ledger(writer="t", ttl=None)
    led.open_pending("kq", why=EVIDENCE, need="等报价", ctx_fp="fp|A")
    led.resolve("kq", verdict=1, reason="报价回来了", ctx_fp="fp|A", journal=jp)

    line = [json.loads(x) for x in open(jp, encoding="utf-8") if x.strip()][-1]
    assert line["t"] == "resolve"
    assert line["had"] is True, "唯一一笔悬位收口，had 却记成 None"

    back = Ledger(writer="r", ttl=None)
    back.replay(jp)
    assert back.stat.get("resolved", 0) == 1, "回放时这笔收口没被算进去"
    assert back.by_verdict("kq") == 1, "收口带出来的那节判决也没了"


# ── ⑤ 六面必须拼回同一节 ─────────────────────────────────────────────

def test_six_port_reads_one_node_not_two():
    led = Ledger(writer="t", ttl=3600)
    led.record("k", 1, "第1圈有分", "fp|A", value_score=9)
    led.record("k", 0, "第2圈没分", "fp|A")

    got, filled = led.six_port("k", "fp|A")
    assert got["④圈"] == 2, "六面没锚在最新一节"
    assert got["②判决"] == 0
    assert got["①值"] is None, "①值从第 1 圈借过来了：六面拼的是两节"
    assert filled == 5, "①值这一圈本来就没有：有分的只有第 1 圈"

    old, _ = led.six_port("k", "fp|A", rnd=1)
    assert old["④圈"] == 1 and old["②判决"] == 1 and old["①值"] == 9


# ── ⑥ 合并撞号：只增不改，不许重排历史 ───────────────────────────────

def test_merge_never_renumbers_existing_rounds(tmp_path):
    """盘上那两节是锚：撞号的是后到的，顺延到队尾；盘上的号一个字不动。

    走真实路径（先存档、第二个进程再存）——「谁是盘上的」只有存盘这一下才说得清。
    """
    snap = str(tmp_path / "m.json")
    jp = snap + ".jsonl"

    mine = Ledger(writer="t", ttl=None)
    mine.record("k", 1, "我的第1圈", "fp|A")
    mine.record("k", 0, "我的第2圈", "fp|A")
    mine.save(snap, journal=jp)

    other = Ledger(writer="o", ttl=None)
    other.record("k", 1, "别人的第1圈", "fp|B")      # 圈号撞车，内容不同
    other.record("k", 0, "别人的第2圈", "fp|B")
    other.save(snap, journal=jp)

    back = Ledger.load(snap, writer="r", ttl=None)
    got = {n["reason"]: n["round"] for n in back.nodes["k"]}
    assert got["我的第1圈"] == 1 and got["我的第2圈"] == 2, f"盘上的号被改了：{got}"
    assert got["别人的第1圈"] == 3 and got["别人的第2圈"] == 4, f"撞进来没顺延：{got}"

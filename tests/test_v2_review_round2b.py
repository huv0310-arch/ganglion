# -*- coding: utf-8 -*-
"""第二轮评审（协作者 C）在修完之后挖到的剩下四处，钉住。

① 流水是正本，正本也要过尺：一行手写的坏判决不许顺着重演进链，更不许一行坏行锁死整本账
② 合并撞号只给**新并进来的**改号，盘上已有的号一个字不动（以前改的正是历史）
③ revisit 也要能落流水，不然短命进程里"判据变过"查不到
④ 三值协议里 processor 返 Pending 是正常答案，不是异常（照协议写反而崩是不能接受的）
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger import Ledger, LedgerGanglion, Pending, EVIDENCE  # noqa: E402


def _line(fp, obj):
    with open(fp, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ── ① 正本过尺 + 坏行隔离 ────────────────────────────────────────────

def test_bad_verdict_in_journal_is_refused(tmp_path):
    jp = str(tmp_path / "j.jsonl")
    _line(jp, {"t": "node", "key": "k", "node": {"key": "k", "verdict": "对", "reason": "手写的",
                                                 "ts": 1.0, "ctx_fp": "", "hits": 0}})
    led = Ledger(writer="r", ttl=3600)
    led.replay(jp)
    assert led.keys() == 0, "手写的坏判决顺着重演进了链"
    assert led.stat.get("bad_lines") == 1


def test_one_bad_line_does_not_lock_the_whole_book(tmp_path):
    jp = str(tmp_path / "j.jsonl")
    good = {"key": "k", "verdict": 1, "reason": "正常", "ts": 1.0, "ctx_fp": "", "hits": 0,
            "round": 1, "value_score": None, "tombstone": None, "last_hit": None, "borrows": []}
    _line(jp, {"t": "node", "key": "k", "node": good})
    _line(jp, {"t": "node", "key": "k2", "node": {"key": "k2", "verdict": 7, "ts": 2.0}})
    _line(jp, {"t": "pending", "key": "k3", "p": {"why": "不是合法质地", "need": "x"}})
    _line(jp, {"t": "node", "key": "k4", "node": dict(good, key="k4", reason="后面这条")})

    led = Ledger(writer="r", ttl=3600)
    led.replay(jp)
    assert led.by_verdict("k") == 1, "一条坏行把前面好好的账也关在门外"
    assert led.by_verdict("k4") == 1, "一条坏行把后面好好的账也关在门外"
    assert led.stat.get("bad_lines") == 2


# ── ② 合并：只给新来的改号 ───────────────────────────────────────────

def test_merge_never_renumbers_the_book_on_disk(tmp_path):
    snap = str(tmp_path / "b.json")
    jp = snap + ".jsonl"
    mine = Ledger(writer="A", ttl=3600)
    for i in (1, 2, 3):
        mine.record("k", 1, f"A-{i}", "fp|A")
    mine.save(snap, journal=jp)

    theirs = Ledger(writer="B", ttl=3600)
    for i in (1, 2):
        theirs.record("k", 1, f"B-{i}", "fp|B")
    theirs.save(snap, journal=jp)

    chain = Ledger.load(snap, writer="r", ttl=3600).nodes["k"]
    assert [(n["round"], n["reason"]) for n in chain][:3] == [(1, "A-1"), (2, "A-2"), (3, "A-3")], \
        "合并把盘上已有的号改了 —— 历史不许动"
    assert [(n["round"], n["reason"]) for n in chain][3:] == [(4, "B-1"), (5, "B-2")]


# ── ③ revisit 落流水 ─────────────────────────────────────────────────

def test_revisit_writes_to_the_journal(tmp_path):
    jp = str(tmp_path / "j.jsonl")
    led = Ledger(writer="w", ttl=3600)
    led.append(jp, "k", 1, "旧判据", "fp|A")      # 底账先落流水
    size = os.path.getsize(jp)
    led.revisit("k", 1, 0, reason="判据变了", journal=jp)
    assert os.path.getsize(jp) > size, "revisit 没落流水"

    back = Ledger(writer="r", ttl=3600)
    back.replay(jp)
    assert len(back.nodes["k"]) == 2 and back.nodes["k"][1]["revisit_of"] == 1


# ── ④ processor 返 Pending 不许崩 ────────────────────────────────────

def test_processor_returning_pending_is_not_an_error(tmp_path):
    jp = str(tmp_path / "j.jsonl")
    led = Ledger(writer="w", ttl=3600)
    gate = LedgerGanglion(ledger=led, journal=jp, why_if_unknown=EVIDENCE)
    out, reason = gate.check("还在跑", lambda c: Pending(EVIDENCE, "等第二批数据"))
    assert isinstance(out, Pending) and out.need == "等第二批数据"
    assert led.size() == 0, "还在走的那一次不占账上的一格"

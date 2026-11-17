# -*- coding: utf-8 -*-
"""并发那一格的验收：多个写者 + 一个常驻压实者，账一行都不许少。

规矩（写在 README / LEDGER.md 里，这里逐条验）：
  · 流水是正本，只追加；快照是压实产物，原子替换。
  · save() 先把别人刚写的行吸收进来再写快照，且**不清流水**。
  · 清流水只有 compact()，它把流水改名留底，不删。
  · 重演幂等：同一节既在快照又在流水里，不许变成两节。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger import Ledger, addr  # noqa: E402


@pytest.fixture
def paths(tmp_path):
    p = str(tmp_path / "book.json")
    return p, p + ".jsonl"


def test_save_absorbs_foreign_lines(paths):
    """常驻进程 save 之前，必须把短命进程刚 append 的行吸收进来。"""
    p, jp = paths
    me = Ledger(writer="t", ttl=3600)
    me.record(addr("常驻这边"), 1, "对", "fp|A")
    me.save(p)

    other = Ledger(writer="t")                       # 短命进程：只写一行，不读全本
    other.append(jp, addr("短命那边"), 0, "另一行", "fp|B")

    me.save(p)                             # 常驻方再存一次
    back = Ledger.load(p, writer="t", ttl=3600)
    assert addr("短命那边") in back.nodes, "别人的行被 save() 吃了"
    assert addr("常驻这边") in back.nodes


def test_save_never_clears_journal(paths):
    p, jp = paths
    led = Ledger(writer="t", ttl=3600)
    led.append(jp, addr("一行"), 1, "对", "fp|A")
    n = os.path.getsize(jp)
    led.save(p)
    assert os.path.getsize(jp) == n, "save() 把流水清了 —— 那是吃账的旧姿势"


def test_save_is_atomic_no_half_book(paths):
    """写一半崩掉的账比缺一笔更坏：快照必须原子替换，不留 .tmp。"""
    p, jp = paths
    led = Ledger(writer="t", ttl=3600)
    led.record(addr("一节"), 1, "对", "fp|A")
    led.save(p)
    assert not os.path.exists(p + ".tmp")
    json.load(open(p, encoding="utf-8"))    # 一定读得动


def test_compact_keeps_a_copy(paths):
    """压实不是"删"：流水**拷一份底**，再把正本截空 —— 出事了还能回头看。

    以前是"改名留底"（正本没了、备份顶着它的名），那样会制造两代文件：
    写者手里的旧句柄还能往备份里写，读的人只看新代，行就静默丢了。
    现在文件自始至终同一个，只截空，没有"换代"。
    """
    p, jp = paths
    led = Ledger(writer="t", ttl=3600)
    led.append(jp, addr("要压实的"), 1, "对", "fp|A")
    bak = led.compact(p)
    assert bak and os.path.exists(bak), "没留底"
    assert os.path.getsize(bak) > 0, "备份里该有那一行"
    assert os.path.exists(jp) and os.path.getsize(jp) == 0, "正本应原地截空，不该整份消失"
    back = Ledger.load(p, writer="t", ttl=3600)
    assert addr("要压实的") in back.nodes


def test_three_writers_one_compactor(paths):
    """三个写者狂写 + 常驻方反复存：一行都不许丢。"""
    p, jp = paths
    keeper = Ledger(writer="t", ttl=3600)
    keeper.save(p)
    total = 0
    for round_ in range(10):
        for w in range(3):
            x = Ledger(writer="t")                    # 每个写者都是独立进程，各自一行
            x.append(jp, addr(f"w{w}-r{round_}"), round_ % 2, f"第{round_}圈", f"fp|{w}")
            total += 1
        keeper.save(p)                      # 每轮都压一次，最容易吃账的位置
    back = Ledger.load(p, writer="t", ttl=3600)
    assert len(back.nodes) == total, f"{total} 行只活下来 {len(back.nodes)} 行"


def test_replay_is_idempotent(paths):
    """同一节在快照和流水里各有一份 —— 重演不许把它记成两节。"""
    p, jp = paths
    led = Ledger(writer="t", ttl=3600)
    led.append(jp, addr("同一节"), 1, "对", "fp|A")
    led.save(p)
    for _ in range(5):
        Ledger.load(p, writer="t", ttl=3600).save(p)
    back = Ledger.load(p, writer="t", ttl=3600)
    assert len(back.nodes[addr("同一节")]) == 1


def test_half_line_waits_for_its_ending(paths):
    """断电留下的半行不猜：这一次当没写过，补全之后才认。"""
    p, jp = paths
    led = Ledger(writer="t", ttl=3600)
    led.save(p)
    with open(jp, "a", encoding="utf-8") as f:
        f.write('{"t":"node","key":"abc"')      # 半行
    back = Ledger.load(p, writer="t", ttl=3600)
    assert len(back.nodes) == 0
    with open(jp, "a", encoding="utf-8") as f:
        f.write(',"node":{"verdict":1,"reason":"补全了","ts":1,"ctx_fp":"","ttl":10,'
                '"key":"abc","hits":0,"last_hit":null,"borrows":[],"round":null,'
                '"value_score":null,"tombstone":null}}\n')
    again = Ledger.load(p, writer="t", ttl=3600)
    assert "abc" in again.nodes

"""ledger_minimal.py —— 最小账本：一把尺子 + 一本账

跑法：python ledger_minimal.py
不用装任何依赖，也不含任何业务判断 —— 只看结构。

七段，就是这本账的全部：
    一  三个值     1 / 0 / 悬位   —— 空是"正在进行"，不是"没有答案"
    二  翻面       第几圈变了      —— 没有圈号就永远看不见
    三  配不上不许借              —— 账里有旧节，情形变了就得重算
    四  借账留痕                  —— 抄了哪一圈、抄的时候核没核
    五  悬位三质地                —— 缺料 / 缺硬证据 / 配不上
    六  落盘回读                  —— 不落盘的账本，重启一次就归零
    七  空不许落账                —— 会当场报错，不给"温和降级"的机会
"""

import os
import tempfile
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))        # 仓库里：examples/ → 上一层
sys.path.insert(0, HERE)                         # 平铺的测试包里：脚本和 ledger.py 同目录

from ledger import addr                                          # noqa: E402  ← 公开的地址函数
from ledger import (Ledger, LedgerGanglion, EVIDENCE, MISMATCH)  # noqa: E402


TMP_JOURNAL = os.path.join(tempfile.mkdtemp(prefix="ledger_demo_"), "book.jsonl")


def fp(*parts):
    """指纹怎么算，你自己定。这里只是最粗的拼法。"""
    return "|".join(str(p) for p in parts)


def t_three_values():
    print("═══ 一、三个值 ═══")
    L = Ledger(writer="demo", ttl=3600)
    g = LedgerGanglion(L, journal=TMP_JOURNAL)

    v1, _ = g.check("这条该放", processor=lambda c: (1, "证据齐"), ctx_fp=fp("店A", "早"))
    v0, _ = g.check("这条该拦", processor=lambda c: (0, "不合格"), ctx_fp=fp("店A", "早"))
    vp, _ = g.check("这条说不准", processor=lambda c: (None, "缺车架号"), ctx_fp=fp("店A", "早"))

    print(f"  判是   → {v1}   落账")
    print(f"  判否   → {v0}   落账")
    print(f"  判不了 → {vp!r}")
    print("           ← 悬位：账上判决栏空着，这一格永远不填 0")
    print(f"  账上共 {L.size()} 节（悬着的那次没占格）")


def t_flips():
    print("\n═══ 二、翻面：同一个地址走五圈 ═══")
    L = Ledger(writer="demo", ttl=3600)
    key = addr("某台车的合规状态")
    for v, why in [(1, "年检有效"), (1, "无变化"), (0, "交强险过期"), (1, "续保完成"), (1, "无变化")]:
        L.record(key, v, reason=why, ctx_fp=fp("车", len(L.nodes.get(key, []))))
    L.backfill_value(key, 3, 0.9)        # 第 3 圈真拦下一台车，事后回填
    print("  圈号 :", L.by_round(key))
    print("  判决 :", [n["verdict"] for n in L.nodes[key]])
    print(f"  认出翻面 {len(L.flips())} 次：")
    for f in L.flips():
        print(f"    第{f['round']}圈  {f['from']} → {f['to']}   因为：{f['why']}")
    print(f"  ①值面取到：{L.by_value(key)}   ← 判决那一刻定不了，只能事后回填")
    print(f"  对面：判 1 的对面是 {L.antipode(1)}、判 0 的对面是 {L.antipode(0)}"
          f"   ← 同一刻成立，不是事后比对")


def t_no_borrow_without_pair():
    print("\n═══ 三、配不上不许借 ═══")
    L = Ledger(writer="demo", ttl=3600)
    key = addr("报价 刹车片")
    L.record(key, 1, reason="有货 380", ctx_fp=fp("店A", "客户在等"))

    n1 = L.probe(key, fp("店A", "客户在等"), journal=TMP_JOURNAL)
    print(f"  情形一样 → 借到第{n1['round']}圈：{n1['reason']}")

    n2 = L.probe(key, fp("店A", "闲聊"), journal=TMP_JOURNAL)
    print(f"  情形变了 → {n2}   原因={L.why_not(key)}   ← 不是空，是该重算")

    n3 = L.probe(key, fp("店B", "客户在等"), journal=TMP_JOURNAL)
    print(f"  换个店   → {n3}   原因={L.why_not(key)}")

    k_new = addr("从没问过")
    print(f"  没问过的 → {L.probe(k_new, fp('x'), journal=TMP_JOURNAL)}   原因={L.why_not(k_new)}")


def t_borrow_trace():
    print("\n═══ 四、借账留痕：抄了哪一圈、核没核 ═══")
    L = Ledger(writer="demo", ttl=3600)
    key = addr("库存 机油")
    L.record(key, 1, reason="有 12 桶", ctx_fp=fp("仓", "今天"))
    L.probe(key, fp("仓", "今天"), checked=False, journal=TMP_JOURNAL)      # 照抄，没核
    L.probe(key, fp("仓", "今天"), checked=True, journal=TMP_JOURNAL)       # 照抄，核过了
    node = L.nodes[key][0]
    print(f"  被翻 {node['hits']} 次，留痕 {len(node['borrows'])} 条：")
    for b in node["borrows"]:
        print(f"    抄第{b['from_round']}圈 · {'核过了' if b['checked'] else '没核'}")
    print("  没核的那一笔也留着 —— 不留痕的借，事后对不上账")


def t_pending_textures():
    print("\n═══ 五、悬位三质地（三个方向，不是三种空）═══")
    got = []
    L = Ledger(writer="demo", ttl=3600, deliver=got.append)
    g = LedgerGanglion(L, journal=TMP_JOURNAL)

    p1, _ = g.check("这条只有半截信息", processor=lambda c: (None, "缺车架号"), ctx_fp=fp("x"))
    p2, _ = g.check("这条只有半截信息", processor=lambda c: (None, "缺车架号"), ctx_fp=fp("x"))
    print(f"  无料 / 无据之空：{p1!r}")
    print(f"    连着悬两次 → tries={p2.tries}（同一个悬位在长，不是新开一条）")
    print(f"    交给上层 {len(got)} 次   ← 空必须有出口，没出口的空等于静默失败")

    key2 = addr("这条账里明明有旧节")
    L2 = Ledger(writer="demo", ttl=3600)
    L2.record(key2, 1, reason="上次合规", ctx_fp=fp("旧情形"))
    L2.probe(key2, fp("新情形"), journal=TMP_JOURNAL)
    print(f"  无配之空：probe 没借到，原因={L2.why_not(key2)}")
    p3 = L2.open_pending(key2, why=MISMATCH, need="情形变了，得重新验", ctx_fp=fp("新情形"))
    print(f"    悬位面取到 {p3!r}  needs_recompute={p3.needs_recompute}")
    print("    ← 这一种别往上交，去重算")


def t_persist():
    print("\n═══ 六、落盘回读 ═══")
    path = os.path.join(HERE, "_ledger_demo.json")
    L = Ledger(writer="demo", ttl=3600)
    key = addr("落盘测试")
    L.record(key, 1, reason="有货", ctx_fp=fp("a"))
    L.open_pending(addr("悬着的"), why=EVIDENCE, need="判据不够硬")
    L.save(path)

    L2 = Ledger.load(path, ttl=3600)
    print(f"  存盘再读：{L2.size()} 节、悬着 {len(L2.unresolved())} 条、判决={L2.by_verdict(key)}")
    os.remove(path)
    print("  进程一停账归零的账本，不算系统，只算一个库")


def t_refuse_writing_nothing():
    print("\n═══ 七、空不许落账（会当场报错）═══")
    L = Ledger(writer="demo", ttl=3600)
    try:
        L.record(addr("判不了的那次"), None, reason="不知道")
    except ValueError as e:
        print(f"  record(None) → ValueError：{e}")


if __name__ == "__main__":
    for fn in (t_three_values, t_flips, t_no_borrow_without_pair, t_borrow_trace,
               t_pending_textures, t_persist, t_refuse_writing_nothing):
        fn()
    print("\n" + "═" * 54)
    print(" 七段全是结构，没有一段是业务。")
    print(" 你要填的只有三个数：窗多长、值怎么打、判不了交给谁。")
    print("═" * 54)

# -*- coding: utf-8 -*-
"""ledger_watchdog.py —— 一个器官怎么把这本账用起来（通用版，零业务）。

这是从真系统里抄出来的形状：每分钟跑一次的探针，判"疼 / 不疼 / 没测到"。

三件事值得抄：

    ① 只记跃迁 —— 状态没变不记账。一天 1440 次判决，全记就是 1440 节；
       只记翻转，平常一天可能是零节。账不爆不是省出来的，是"不值得记的不记"。
    ② 只写流水 —— 每轮 append 一行，不读全本、不重写全本。短命进程就该这样。
    ③ 账本挂了不许拖垮器官 —— except 包住，器官照跑。账是副产物，不是命。

跑：
    python examples/ledger_watchdog.py            # 模拟 12 轮：正常→疼→正常→失明→恢复
    python examples/ledger_watchdog.py --show     # 只看账（这就是读取的一方）
"""
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ganglion import _make_key
from ledger import Ledger, EVIDENCE

BOOK = os.path.join(tempfile.gettempdir(), "watchdog_ledger.json")   # 快照
JOURNAL = BOOK + ".jsonl"                                            # 流水
ADDR = _make_key("这台机器疼不疼")


# ── 器官 ────────────────────────────────────────────────────
def sense(round_no):
    """假装是探测器。返回 (读数, 差什么)：读数 None 就是没测到。"""
    script = {0: 0.004, 1: 0.006, 2: 0.031, 3: 0.028, 4: 0.005,
              5: None, 6: None, 7: 0.004, 8: 0.004}[round_no % 9]
    if script is None:
        return None, "取样超时"
    return script, None


def tick(st, round_no, threshold=0.02):
    """一轮：判、必要时记、返回这一轮做了什么。"""
    v, gap = sense(round_no)

    # 没测到 = 第三种质地的空，不落判决栏，只悬着
    if gap:
        if not st.get("blind"):                       # 刚瞎：悬一处
            book("blind", gap)
            st["blind"] = True
        return "没测到：" + gap

    if st.get("blind"):                               # 眼睛回来了：销悬位
        book("resolve")
        st["blind"] = False

    hit = v > threshold
    if hit and not st.get("pain"):                    # 不疼 → 疼：跃迁才记
        book("yes", "峰值 %.4fs > %.2fs" % (v, threshold))
        st["pain"] = True
        return "疼了"
    if not hit and st.get("pain"):                    # 疼 → 不疼：对面显形
        book("no", "回到 %.4fs" % v)
        st["pain"] = False
        return "恢复了"
    return "没变"


# ── 记账（唯一碰账本的地方） ─────────────────────────────────
def book(kind, reason=""):
    try:
        L = Ledger(writer="watchdog")                                  # 不读全本
        if kind == "blind":
            L.append(JOURNAL, ADDR, None, reason, "local", why=EVIDENCE)
        elif kind == "resolve":
            L.resolve(ADDR, journal=JOURNAL)          # 销悬位，不算判决
        else:
            L.append(JOURNAL, ADDR, 1 if kind == "yes" else 0, reason, "local")
    except Exception:
        pass                                          # 账本死了，器官不许跟着死


# ── 读的一方 ────────────────────────────────────────────────
def show():
    if not (os.path.exists(BOOK) or os.path.exists(JOURNAL)):
        print("还没有账")
        return
    L = Ledger.load(BOOK)                             # 快照 + 回放流水
    nodes = L.nodes.get(ADDR, [])
    print("地址:", ADDR)
    for n in nodes:
        print("  第 %d 圈  判决 %s  凭什么：%s" %
              (n["round"], n["verdict"], n.get("reason", "")))
    print("合计 %d 节 · 对面显形 %d 次 · 悬着 %d 处 · 回放 %d 行"
          % (len(nodes), len(L.flips(ADDR)), len([x for x in L.open if x == ADDR]),
             L.stat.get("replayed", 0)))


def main(argv):
    if "--show" in argv:
        show()
        return 0
    for f in (BOOK, JOURNAL):
        if os.path.exists(f):
            os.remove(f)
    st = {}
    for i in range(9):
        print("第 %d 轮 %-14s %s" % (i + 1, tick(st, i), ""))
    print()
    show()
    print()
    print("注意：9 轮里只记了 %d 节 —— 不是每轮都值得记。" % len(Ledger.load(BOOK).nodes.get(ADDR, [])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

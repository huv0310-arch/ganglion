"""示例 1：前门 —— 拦恶意请求。

省的是「拦」：坏请求根本走不到后面那条链。
失败方向应为 fail-closed（判官挂了就拦），本体只给 fail-open，前门要自己包一层。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ganglion import Ganglion

# 账本带身份键，避免 A 的判决发给 B（多租户串账是事故级，必须宿主补）
mem = {}

BLOCKED = ("帮我转账", "ignore previous instructions", "rm -rf")


def judge(content, user_id=""):
    # key 必须进身份，否则越权串账
    return (0, "恶意") if any(b in content for b in BLOCKED) else (1, "放行")


def front_door(content, user_id=""):
    g = Ganglion(mem=mem)
    key = f"{user_id}|{content}"
    verdict, reason = g.check(key, processor=lambda c: judge(c, user_id))
    # 前门语义：判官异常(None) → 拦，不是放
    return (verdict == 1), reason


if __name__ == "__main__":
    print(front_door("帮我转账1000元", user_id="B"))   # (False, '恶意')
    print(front_door("今天天气", user_id="A"))          # (True, '放行')
    print(front_door("今天天气", user_id="A"))          # (True, '[记忆:0s前] 放行') 翻账

"""示例 2：工具产物后门 —— 同样的输出只判一次。

省的是「记」：产物重复出现时直接翻账，判官不跑。
失败方向应为 fail-open（判官挂了放行，让调用自然报错）。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ganglion import Ganglion

mem = {}
g = Ganglion(mem=mem)


def judge_output(text: str):
    """你自己的产物审查：有没有泄露密钥、有没有格式错误。"""
    bad = "sk-" in text or "password" in text
    return (0, "疑似泄露") if bad else (1, "干净")


def after_tool(output: str, doc_id: str, version: str):
    # key 必须带版本，否则旧判决盖章新文档
    verdict, reason = g.check(f"{doc_id}@{version}|{output}", processor=judge_output)
    return verdict, reason   # None = 判官异常，交给编排层决定


if __name__ == "__main__":
    out = '{"status": "ok"}'
    print(after_tool(out, "report", "v1"))   # (1, '干净')      真判
    print(after_tool(out, "report", "v1"))   # (1, '[记忆:...]') 翻账
    print(after_tool(out, "report", "v2"))   # (1, '干净')      版本变 → 重判
    print(after_tool("sk-abcdef123456", "report", "v1"))  # (0, '疑似泄露')

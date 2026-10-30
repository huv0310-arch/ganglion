"""示例 3：健康探针 —— 状态广播器，不是放行闸门。

正确形状：后台常驻探测写共享状态 → 调用前只读快照。
判决只能用于「提前告知」，不能当「放行凭据」（TOCTOU 窗口）。
"""

import time

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ganglion import Ganglion

shared = {"ok": True, "msg": "通"}


def probe():
    return shared["ok"], shared["msg"]


# 探针型神经节：check 忽略 content，只跑 probe_fn
gate = Ganglion.health_check("payment", probe)


def call_payment_api():
    verdict, reason = gate.check(None)     # 读快照，零额外探测
    if verdict != 1:
        raise RuntimeError(f"降级: {reason}")   # 编排层短路，别让模型自由发挥
    return "实际调用"


if __name__ == "__main__":
    print(call_payment_api())              # 实际调用

    shared.update(ok=False, msg="超时")     # 服务挂了
    print(gate.check(None))                # (0, 'payment: 超时')
    try:
        call_payment_api()
    except RuntimeError as e:
        print("降级分支:", e)

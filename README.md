# Ganglion · 神经节

> **发布说明（2026-10-31）**：本仓库只放**定稿**。
> 日常开发、试跑、未定稿的判决账本都在私有库，定稿后才出现在这里。
> 当前公开的是 **V1 原件**：105 行，一字未改（tag `v0-original`，2026-10-30 13:36 首次提交）。

> **一个会记账的判决件。** 判过的记在账上，保鲜期（默认 1 小时）内不再判第二次。
> 105 行纯标准库，零依赖，`import` 即用。

[![tests](https://img.shields.io/badge/tests-19%20passed-brightgreen)](#测试)
[![python](https://img.shields.io/badge/python-3.8+-blue)](#)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

```
verdict: 1 = 通过 · 0 = 有问题 · None = 未决
```

---

## 它解决什么

AI 系统里最贵的不是算一次，是**把同一件事算两遍**。
同一个文件、同一条消息、同一个参数——判过一次就该记住。

Ganglion 把判决记进账本：命中直接翻账（零成本、微秒级），没命中才真判。
**它不产生更好的判决，只让已有判决变得更便宜、可复用。**
（「谁判的、翻过几次、判错了怎么翻」这些属于上层账本，不在这一版；这一版只管「判过没」。）

## 用法

```python
from ganglion import Ganglion

def my_judge(content):
    # 你自己的判断逻辑，返回 (verdict, reason)
    return (1, "放行") if "病毒" not in content else (0, "拦截")

# 带记忆（mem 就是个 dict；内存 dict 进程一停就归零，要跨进程请传自己的持久化 dict）
g = Ganglion(mem={}, ttl=3600)
g.check("你好", processor=my_judge)     # → (1, "放行")      真判
g.check("你好", processor=my_judge)     # → (1, "[记忆:0s前] 放行")  翻账，不重判

# 不带记忆（纯反射）
g = Ganglion()
```

### 批量

```python
results = g.scan(["a", "b", "c"], processor=my_judge)
# [(key, verdict, reason), ...]
```

### 健康探针

```python
probe = Ganglion.health_check("DB", lambda: (True, "通"))
probe.check(None)   # → (1, "DB: 通")
```

## 三条硬设计

| 设计 | 为什么 |
|------|--------|
| **0/1/None 三值** | 「没判出来」和「判出来是坏的」是两回事，合并会骗人 |
| **fail-open 不落账** | processor 抛异常 → 返回 `None` 且**不写记忆**。瞬时故障不会把坏判决固化一小时 |
| **`None` 也算结论** | processor 主动返回 `None`（「这次判不了」）会照常记账——它同样是一个结论，只是不是 0 |
| **骨里一个场景不放** | key 算法、三值语义、过期逻辑是结构；具体判什么，全交给你写的 processor |

**通用性来自「什么都不假设」**：只要求一个 dict、一个 callable。
不假设框架、不假设 I/O、不假设 ID、不假设生命周期。

## 实测（TRAE 独立测试，2026-10-30）

近 30 项断言：**28 项复现**，1 条文档示例不成立，1 条措辞被反例推翻。

| 场景 | 结果 |
|------|------|
| 8 个挂载点（消息入口/工具前/工具后/探针/索引/前门/批量/异步） | **全部通过，零改代码** |
| 工具产物 20 次同查询 | 736ms·20 次实判 → 153ms·1 次实判，**省 79%** |
| 前门拦 200 请求 | 80s → 56s，**省 30%** |
| 前门+后门夹心 | 3164ms → 2396ms，**省 24%** |

> "它强在『放在哪都能用』，不强在『什么都能判』。
> 通用性是从『什么都不假设』里长出来的，不是从『什么都能做』里长出来的。"

**评级**：带记忆的判决原语 **A−**；自称万能件 **B**。
多租户串账、版本漂移两处属事故级，**由宿主补 key，本体刻意不管**。

## 测试

```
python -m unittest discover -s tests -v
```

19 条，零依赖。其中 `test_exception_not_written` 钉住 fail-open 不落账。

## 冻结

`ganglion.py` 自 2026-10-29 起**冻结**，105 行一行未改（tag `v0-original`）。
冻结的意义：判决的结构不该随场景漂移——算法会进化，结构不该。

已知两处瑕疵，**按冻结规矩不在原件上改**，留给下一版标准件处理：

1. 第 23 行的正则字符串有无效转义 `\[`，Python 3.12+ 报 `SyntaxWarning`（将来版本会变成硬错误）——不影响当前行为，写法要改成纯 raw string。
2. `processor` 主动返回 `None` 时会被记账，等于把「暂时判不了」固化一个保鲜期；标准件应把它标成 pending 而不是当结论存。

需要新能力时另立新文件，不覆盖这一版。

## License

MIT · © 2026 怼怼胡 (duiduihu)

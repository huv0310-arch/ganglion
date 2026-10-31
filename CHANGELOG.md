# 变更记录

格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

本文件只记录**公开发布的版本**。日常开发、试跑、未定稿内容不进本库，因此不出现在这里。

## [0.1.0] — 2026-10-30

首个公开版本：判决原语本体。

### 新增
- `ganglion.py` —— 105 行判决原语。一个 content 进，三个值出（`1` 通过 / `0` 有问题 / `None` 未决）；带 TTL 的记忆账；processor 抛异常时 fail-open 不落账。
- `examples/` —— 三个挂载点示例：消息入口、工具产物、健康探针。
- `tests/` —— 22 条测试，零依赖。
- `evidence/` —— 三段 SHA256 联合存证（SOURCE / PAPER / COMBINED）与时间锚说明。

### 冻结
- `ganglion.py` 自 2026-10-29 起冻结，105 行一字未改。锚点 tag `v0-original`（2026-10-30 13:36 首次提交）。
- `tests/test_frozen_original.py` 钉住 sha256、行数、以及「骨里不出现业务词」——改动会当场变红。

### 已知问题
- 第 23 行正则字符串含无效转义 `\[`：Python 3.12+ 报 `SyntaxWarning`，将来版本会成为硬错误。当前行为正确。
- `processor` 主动返回 `None` 时会照常记账，等于把「暂时判不了」固化一个保鲜期。

以上两条按冻结规矩**不在原件上改**，留给下一版标准件。

### 文档修正
- README 中四处与代码不一致的说法已改正：默认 TTL 下「永远不再判」不成立；「可审计、可回放」不属于本版能力；fail-open 未覆盖 `None` 记账；`mem` 是内存 dict、进程结束即归零。

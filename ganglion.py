"""神经节 v0 —— 万能标准件
任何地方插上就能用。记忆随时接（传 dict 或不传）。

用法:
    g = Ganglion(mem={})                    # 带记忆
    g = Ganglion()                          # 不带记忆（纯反射）
    g = Ganglion(mem=my_dict, ttl=3600)     # 带记忆+保鲜期

    result = g.check("消息内容", processor=my_func)
    # result = (verdict, reason)
    #   verdict: 1=通过 0=有问题 None=未决
    #   reason:  人类可读的一句话

    processor 是你自己写的判断函数: fn(content) -> (verdict, reason)
"""

import hashlib, time, re

# 默认保鲜期（秒）
_DEFAULT_TTL = 3600

# 剥噪声：去掉 @名、标点、多余空白，取前64字符做key
_NOISE = re.compile(r'[@＠\s，。！？、；：""''【】\[\]{}()（）!?.,:;\'"`~\-]+')


def _make_key(content: str) -> str:
    """内容 → 哈希键。剥噪声取前64字符，sha256前8字节。"""
    clean = _NOISE.sub('', str(content or ''))[:64]
    return hashlib.sha256(clean.encode('utf-8')).hexdigest()[:16]


class Ganglion:
    """万能神经节。

    mem: dict，key=_make_key的哈希值，value=(verdict, reason, timestamp)
         不传或None = 无记忆模式（每次重新算）
    ttl: 保鲜期秒数，超过就重新算。0 = 永不过期（不推荐）
    """

    def __init__(self, mem=None, ttl=_DEFAULT_TTL):
        self.mem = mem          # None = 不记忆
        self.ttl = ttl

    def check(self, content, processor=None):
        """主入口。返回 (verdict, reason)。

        verdict: 1=通过 / 0=有问题 / None=未决(需要上层处理)
        """
        if processor is None:
            processor = self._default_processor

        key = _make_key(content) if self.mem is not None else None
        now = time.time()

        # ── 翻书：查记忆（有 mem 才翻）──
        if key is not None and key in self.mem:
            verdict, reason, stored_at = self.mem[key]
            age = now - stored_at
            if self.ttl == 0 or age < self.ttl:
                return verdict, f"[记忆:{int(age)}s前] {reason}"

        # ── 叠加态：没命中或过期 → 实际检测 ──
        try:
            verdict, reason = processor(content)
        except Exception as e:
            # fail-open：检测失败不阻塞，当未决处理
            return None, f"神经节检测异常: {e}"

        # ── 打标：写回记忆（有 mem 才写）──
        if self.mem is not None:
            self.mem[key] = (verdict, reason, now)
        return verdict, reason

    @staticmethod
    def _default_processor(content):
        """默认处理器：纯占位。传你自己的 processor。"""
        return None, "无处理器"

    # ── 辅助：批量检查 ──
    def scan(self, items, processor=None):
        """一次扫多个内容。返回 [(key, verdict, reason), ...]"""
        results = []
        for item in items:
            v, r = self.check(item, processor)
            key = _make_key(item)
            results.append((key, v, r))
        return results

    # ── 辅助：健康探针型神经节（装在模块前面用） ──
    @staticmethod
    def health_check(name, probe_fn):
        """快速创建健康探针型神经节。

        probe_fn: fn() -> (bool, str)  # (通不通, 说明)
        返回一个 Ganglion，check(content) 忽略content，只跑probe_fn。
        """
        g = Ganglion()
        def _probe(_content):
            try:
                ok, msg = probe_fn()
                return (1 if ok else 0), f"{name}: {msg}"
            except Exception as e:
                return 0, f"{name}: 探针异常 {e}"
        g.check = lambda content=None, processor=None: _probe(content)
        return g

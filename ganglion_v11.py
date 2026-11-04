"""神经节 v1.1 —— 万能标准件（改骨四条；原件 ganglion.py 冻结不动）

用法与 v0 一致:  g = Ganglion(mem={}) 或 Ganglion(path="g.json")（跨进程文件缓存，写带锁）
    verdict, reason = g.check("内容", processor=my_func)      # 1=通过 0=有问题 None=未决
    g.last_age = 本次复用的旧结果年龄秒（没复用 = None）

四条改动:
    ① 并发写保护：文件缓存写时加锁 + 锁内重读重写 + os.replace 原子落盘，多进程不互覆丢写
    ② 吞错留痕：fail-open 语义不变（绝不抛），异常写入同目录 ganglion_v11.error.log
    ③ 默认 ttl 3600→1800：四档重放实测取值
    ④ 借出带账龄：命中时 reason 带 [记忆:Ns前]，同时写 self.last_age，调用方自判信不信
"""
import hashlib, json, os, time, re

try:
    import msvcrt                        # Windows 文件锁
except ImportError:
    msvcrt = None
try:
    import fcntl                         # POSIX 文件锁
except ImportError:
    fcntl = None

_DEFAULT_TTL = 1800                      # ③ 3600→1800：更短保鲜期，过时结果更快让位
_ERROR_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ganglion_v11.error.log")   # ② 同目录留痕
_NOISE = re.compile(r"""[@＠\s，。！？、；：""''【】\[\]{}()（）!?.,:;'\"`~\-]+""")   # key 公式 v0 原样，改了记忆就清零


def _make_key(content):
    """内容 → 哈希键。剥噪声取前64字符，sha256前8字节。"""
    clean = _NOISE.sub('', str(content or ''))[:64]
    return hashlib.sha256(clean.encode('utf-8')).hexdigest()[:16]


def _log_error(action, exc):
    """② 留痕：时间戳 + 动作 + 异常类型。写日志本身也绝不抛。"""
    try:
        with open(_ERROR_LOG, "a", encoding="utf-8") as fh:
            fh.write("%s | %s | %s: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), action, type(exc).__name__, exc))
    except Exception:
        pass


def _lock_ex(fh):
    """拿锁：Windows msvcrt / POSIX fcntl；都没有就退化为不锁（照常读写）。"""
    if msvcrt is not None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)     # 阻塞到拿到（约10s上限）
    elif fcntl is not None:
        fcntl.flock(fh, fcntl.LOCK_EX)


def _unlock(fh):
    if msvcrt is not None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    elif fcntl is not None:
        fcntl.flock(fh, fcntl.LOCK_UN)


def _read_json(path, lock=False):
    """读文件缓存：不存在=空；坏了=当空+留痕（fail-open）。

    lock=True 先拿锁再开文件——Windows 下读写同开会让对面 os.replace 报 WinError 5。
    """
    fh = None
    try:
        if lock:
            fh = open(path + ".lock", "a+")
            _lock_ex(fh)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        _log_error("load:%s" % os.path.basename(path), e)
        return {}
    finally:
        if fh is not None:
            try:
                _unlock(fh)
                fh.close()
            except Exception as e:
                _log_error("unlock:%s" % os.path.basename(path), e)


def _locked_store(path, key, value):
    """① 并发写保护：锁内【重读→改→原子替换】，多进程同写不丢。失败留痕不抛。"""
    fh = None
    try:
        fh = open(path + ".lock", "a+")
        if msvcrt is not None:                       # Windows：阻塞拿锁（约10s上限）
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        elif fcntl is not None:                      # POSIX
            fcntl.flock(fh, fcntl.LOCK_EX)
        data = _read_json(path)                      # 已在锁内，不用再拿锁
        data[key] = value
        tmp = "%s.tmp%d" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as out:
            json.dump(data, out, ensure_ascii=False)
        for i in range(3):                           # Windows 上偶发占用，让一下再试
            try:
                os.replace(tmp, path)                # 原子落盘，读侧永远读到整本
                break
            except PermissionError:
                if i == 2:
                    raise
                time.sleep(0.02)
    except Exception as e:
        _log_error("store:%s" % os.path.basename(path), e)
    finally:
        if fh is not None:
            try:
                _unlock(fh); fh.close()
            except Exception as e:
                _log_error("unlock:%s" % os.path.basename(path), e)


class Ganglion:
    """万能神经节 v1.1。mem=同进程记忆dict；ttl=保鲜秒（0=永不过期）；path=文件缓存。"""

    def __init__(self, mem=None, ttl=_DEFAULT_TTL, path=None):
        self.mem, self.ttl, self.path = mem, ttl, path
        self.last_age = None                       # ④ 本次复用的旧结果年龄（秒）；没复用 = None

    def _load(self):
        # 拿锁读：Windows 下读侧开着文件会让写侧 os.replace 报 WinError 5
        return _read_json(self.path, lock=True) if self.path is not None else self.mem

    def _store(self, key, verdict, reason, now):
        if self.path is not None:
            _locked_store(self.path, key, [verdict, reason, now])
        elif self.mem is not None:
            try:
                self.mem[key] = (verdict, reason, now)
            except Exception as e:
                _log_error("store:mem", e)

    def check(self, content, processor=None):
        """主入口。返回 (verdict, reason)；verdict: 1=通过 / 0=有问题 / None=未决。"""
        if processor is None:
            processor = self._default_processor
        mem = self._load()
        key = _make_key(content) if mem is not None else None
        now, self.last_age = time.time(), None

        # ── 翻书：查记忆 ──
        if key is not None and key in mem:
            verdict, reason, stored_at = mem[key]
            age = now - stored_at
            if self.ttl == 0 or age < self.ttl:
                self.last_age = age                # ④ 复用带年龄
                return verdict, "[记忆:%ds前] %s" % (int(age), reason)

        # ── 叠加态：没命中或过期 → 实际检测 ──
        try:
            verdict, reason = processor(content)
        except Exception as e:
            # fail-open：检测失败不阻塞，当未决处理；② 但必须留痕
            _log_error("check:%s" % repr(str(content)[:32]), e)
            return None, "神经节检测异常: %s" % e

        # ── 打标：写回记忆（异常判决不写，降级不冻结）──
        if mem is not None:
            self._store(key, verdict, reason, now)
        return verdict, reason

    @staticmethod
    def _default_processor(content):
        """默认处理器：纯占位。传你自己的 processor。"""
        return None, "无处理器"

    def scan(self, items, processor=None):
        """一次扫多个内容。返回 [(key, verdict, reason), ...]"""
        return [(_make_key(item),) + self.check(item, processor) for item in items]

    @staticmethod
    def health_check(name, probe_fn):
        """健康检查型神经节：check(content) 忽略内容，只跑 probe_fn。"""
        g = Ganglion()

        def _probe(_content):
            try:
                ok, msg = probe_fn()
                return (1 if ok else 0), "%s: %s" % (name, msg)
            except Exception as e:
                _log_error("probe:%s" % name, e)   # ② 检查异常也留痕
                return 0, "%s: 检查异常 %s" % (name, e)

        g.check = lambda content=None, processor=None: _probe(content)
        return g

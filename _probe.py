
import sys; sys.path.insert(0,"D:/Users/Administrator/.pubcheck")
import ganglion, re
print("编译后的 pattern 长度:", len(ganglion._NOISE.pattern))
print("pattern 尾部 40 字符:", repr(ganglion._NOISE.pattern[-40:]))
cases = ["京A12345","京Ａ１２３４５","京A 12345","Hello, world!","Hello world","Helloworld",
         'He said “yes”.', 'He said "yes".', "【告警】电池过热！！","告警 电池过热",
         "电池过热（99度）","电池过热99度"]
for c in cases:
    print(f"{c!r:26} -> key={ganglion._make_key(c)!r}")

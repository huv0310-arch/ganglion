
import sys; sys.path.insert(0,"D:/Users/Administrator/.pubcheck")
from ganglion import Ganglion
# 1) "判过一次，永远不再判" 在默认 ttl 下不成立
g = Ganglion(mem={}); calls=[0]
def judge(c):
    calls[0]+=1; return 1,"ok"
g.check("同一件事", judge); g.check("同一件事", judge)
print("默认ttl两次调用实判次数:", calls[0], "| 账本条数:", len(g.mem))
# 2) 过期后重判
import time
g.mem[list(g.mem)[0]] = (1,"ok",time.time()-99999)
g.check("同一件事", judge); print("过期后实判累计:", calls[0])
# 3) 未决(None)也被记进账本
g2 = Ganglion(mem={})
print("processor返回None:", g2.check("判不了的活", lambda c:(None,"暂时不确定")))
print("账本内容:", g2.mem)
# 4) 记账是否持久
import subprocess
print("mem 是内存 dict，进程重启即归零（类型:%s）" % type(g2.mem).__name__)

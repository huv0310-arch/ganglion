#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
render_certificate.py — 重出「原创凭证 · 三段 SHA256 联合存证」图。

设计约束（改动须同步更新 evidence/EVIDENCE.md）：
  1. 输入参数全部写死在本文件顶部，不读环境、不读时间、不联网。
  2. 不写入任何 PNG 元数据（无 tEXt / tIME），保证同一 PIL 版本下字节完全一致。
  3. 运行后打印 sha256，与 EVIDENCE.md 里登记的新图哈希比对；对不上即为改动过。

用法：
    python evidence/render_certificate.py            # 输出到 evidence/ 下
    python evidence/render_certificate.py --out x.png

为什么要有这个文件：
    PAPER 那枚指纹栽在「只记了结果、没记对什么算的」，从此不可复算。
    图不能再栽一次——所以图的生产方式必须落盘、可重跑、可复算。
"""

import hashlib
import os
import sys

from PIL import Image, ImageDraw, ImageFont

# ── 写死的输入（不许改成运行时读取）────────────────────────────────
SOURCE_HEX = "7afe4b7ff4af35dda67d506b9ac2e5dc215170f748e6d574f3c1bf198e38f047"
PAPER_HEX = "db2be031395a6b46089638e5094dcc467baec01b4dd03a8cd03b5756defe2f55"
COMBINED_HEX = "cf1a8a6d37948be1e40b4e9941c875d65b614dc87da2bb480d7dbf482c974ce6"

SOURCE_LABEL = "SHA256(ganglion.py) — 105 lines · frozen 2026-10-29"
PAPER_LABEL = "SHA256(canonical citation)"
PAPER_DETAIL = "DOI:10.1126/science.adz5323 · Science · 2026-10-15"  # 更正版：原图误写 10-16
COMBINED_LABEL = "SHA256(SOURCE || PAPER) — 联合指纹，一证双挂"

AUTHOR = "怼怼胡"
CANVAS_W, CANVAS_H = 1600, 1130

FONT_CN = r"C:/Windows/Fonts/msyh.ttc"
FONT_CN_BD = r"C:/Windows/Fonts/msyhbd.ttc"
FONT_MONO = r"C:/Windows/Fonts/consola.ttf"

BG = (250, 250, 248)
INK = (26, 26, 26)
MUTED = (110, 110, 105)
ACCENT = (35, 78, 55)
CARD_BG = (255, 255, 255)
CARD_LINE = (222, 222, 214)


def hex_pairs(h):
    """64 位十六进制 → 两行，便于人眼比对。"""
    return h[:32], h[32:]


def build(path):
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), BG)
    d = ImageDraw.Draw(img)

    f_title = ImageFont.truetype(FONT_CN_BD, 46)
    f_sub = ImageFont.truetype(FONT_CN, 25)
    f_small = ImageFont.truetype(FONT_CN, 19)
    f_label = ImageFont.truetype(FONT_CN_BD, 23)
    f_mono = ImageFont.truetype(FONT_MONO, 21)
    f_foot = ImageFont.truetype(FONT_CN, 17)

    m = 80

    # 标题区
    d.text((m, 52), "Ganglion — Originality Certificate", font=f_title, fill=INK)
    d.text((m, 118), "原创性凭证 · 结构代码与文献指纹联合存证", font=f_sub, fill=MUTED)
    d.text((m, 160), "作者：%s" % AUTHOR, font=f_small, fill=MUTED)
    d.line([(m, 200), (CANVAS_W - m, 200)], fill=CARD_LINE, width=2)

    # 三段卡片
    blocks = [
        ("①", "SOURCE", SOURCE_LABEL, None, SOURCE_HEX),
        ("②", "PAPER", PAPER_LABEL, PAPER_DETAIL, PAPER_HEX),
        ("③", "COMBINED", COMBINED_LABEL, None, COMBINED_HEX),
    ]

    y = 232
    block_h = 232
    for idx, tag, name, detail, hexv in blocks:
        d.rectangle([m, y, CANVAS_W - m, y + block_h - 24], fill=CARD_BG, outline=CARD_LINE, width=2)
        d.text((m + 30, y + 22), "%s  %s" % (tag, name), font=f_label, fill=ACCENT)
        # 说明行：只在标题行尾追加 detail，正文不再重复
        label_end = m + 30 + d.textlength("%s  %s  " % (tag, name), font=f_label)
        if detail:
            d.text((label_end, y + 25), detail, font=f_small, fill=MUTED)
        line1 = hex_pairs(hexv)[0]
        line2 = hex_pairs(hexv)[1]
        d.text((m + 30, y + 108), line1, font=f_mono, fill=INK)
        d.text((m + 30, y + 148), line2, font=f_mono, fill=INK)
        y += block_h

    # 声明
    d.line([(m, 962), (CANVAS_W - m, 962)], fill=CARD_LINE, width=2)
    d.text((m, 986),
           "ganglion.py is original work by %s; the citation above is the sole structural reference." % AUTHOR,
           font=f_small, fill=INK)
    d.text((m, 1016),
           "本凭证由三段 SHA256 联合构成：改动任一字节，三枚指纹同时失效。",
           font=f_small, fill=INK)
    d.text((m, 1058),
           "verified locally · reproduce: sha256sum ganglion.py · combined = sha256(SOURCE||PAPER)",
           font=f_foot, fill=MUTED)

    img.save(path, format="PNG", optimize=False, compress_level=6)
    return path


def main():
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "原创凭证_指纹联合存证.png")
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1]

    if not os.path.exists(FONT_CN):
        sys.exit("字体缺失：%s" % FONT_CN)

    build(out)
    raw = open(out, "rb").read()
    print("输出 :", out)
    print("字节 :", len(raw))
    print("sha256:", hashlib.sha256(raw).hexdigest())
    print("尺寸 : %dx%d" % (CANVAS_W, CANVAS_H))


if __name__ == "__main__":
    main()

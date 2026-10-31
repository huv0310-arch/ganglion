"""冻结守卫：确认 V1 原件一个字都没被改过。

这几条不是测功能，是测"历史"。任何人（包括我们自己）动了 ganglion.py，
这里会先红，而不是等外人拿 sha256 来对。
"""
import hashlib
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
ORIGINAL = ROOT / "ganglion.py"

# 2026-10-30 首次提交（0a6a6f6，tag v0-original）时的指纹
EXPECTED_SHA256 = "7afe4b7ff4af35dda67d506b9ac2e5dc215170f748e6d574f3c1bf198e38f047"
EXPECTED_LINES = 105


class TestOriginalIsFrozen(unittest.TestCase):
    def test_sha256_unchanged(self):
        digest = hashlib.sha256(ORIGINAL.read_bytes()).hexdigest()
        self.assertEqual(digest, EXPECTED_SHA256, "ganglion.py 被改过了——原件冻结，新能力请另开文件")

    def test_line_count(self):
        n = len(ORIGINAL.read_text(encoding="utf-8").splitlines())
        self.assertEqual(n, EXPECTED_LINES, "105 行是这件原件的身份证，行数变了说明动过")

    def test_no_business_words_inside(self):
        """骨里不应该出现任何行业词——通用性靠'减'出来。"""
        src = ORIGINAL.read_text(encoding="utf-8").lower()
        for word in ("dms", "okx", "车", "订单", "报价", "交易", "wechat"):
            self.assertNotIn(word, src, f"骨里出现了业务词 {word!r}")


if __name__ == "__main__":
    unittest.main()

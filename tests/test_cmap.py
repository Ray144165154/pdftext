"""ToUnicode CMap 解析测试。

重点覆盖 **bfrange 的数组写法** ``<lo> <hi> [<d1> <d2> ...]``——
很多实现只认递增写法 ``<lo> <hi> <base>``，遇到数组写法就整段乱码。
"""

from __future__ import annotations

import unittest

from pdfbuilder import (
    tounicode_bfchar,
    tounicode_bfrange,
    tounicode_bfrange_array,
)

from pdftext.cmap import decode_utf16be, parse_tounicode


class TestDecodeUtf16be(unittest.TestCase):
    def test_ascii(self):
        self.assertEqual(decode_utf16be(b"\x00A"), "A")

    def test_bmp_cjk(self):
        self.assertEqual(decode_utf16be(b"\x4e\x2d"), "中")
        self.assertEqual(decode_utf16be(b"\x65\x87"), "文")

    def test_multi_character_mapping(self):
        # 连字：一个码位映射到多个字符
        self.assertEqual(decode_utf16be(b"\x00f\x00i"), "fi")

    def test_surrogate_pair(self):
        # U+1F600 用代理对表示
        self.assertEqual(decode_utf16be(b"\xd8\x3d\xde\x00"), "\U0001f600")

    def test_lone_surrogate_dropped(self):
        self.assertEqual(decode_utf16be(b"\xd8\x3d"), "")

    def test_odd_length_tolerated(self):
        self.assertEqual(decode_utf16be(b"\x00A\x00"), "A")

    def test_empty(self):
        self.assertEqual(decode_utf16be(b""), "")


class TestParseToUnicode(unittest.TestCase):
    def test_empty_input(self):
        cmap = parse_tounicode(b"")
        self.assertEqual(len(cmap), 0)

    def test_bfchar_pairs(self):
        data = tounicode_bfchar([(1, "A"), (2, "B"), (0x10, "中")])
        cmap = parse_tounicode(data)
        self.assertEqual(cmap.get(1), "A")
        self.assertEqual(cmap.get(2), "B")
        self.assertEqual(cmap.get(0x10), "中")

    def test_bfchar_multi_char_value(self):
        data = tounicode_bfchar([(5, "fi")])
        self.assertEqual(parse_tounicode(data).get(5), "fi")

    def test_bfrange_incrementing(self):
        # 16..18 依次映射到 U+4E2D 起连续码位
        cmap = parse_tounicode(tounicode_bfrange(16, 18, 0x4E2D))
        self.assertEqual(cmap.get(16), "\u4e2d")
        self.assertEqual(cmap.get(17), "\u4e2e")
        self.assertEqual(cmap.get(18), "\u4e2f")

    def test_bfrange_array_form(self):
        """数组写法：``<lo> <hi> [<d1> <d2> <d3>]``——不支持就会整段丢字。"""
        cmap = parse_tounicode(tounicode_bfrange_array(1, 3, ["中", "文", "字"]))
        self.assertEqual(cmap.get(1), "中")
        self.assertEqual(cmap.get(2), "文")
        self.assertEqual(cmap.get(3), "字")

    def test_bfrange_array_form_partial(self):
        # 数组项少于区间长度时，只映射给出的部分，不应崩
        cmap = parse_tounicode(tounicode_bfrange_array(1, 5, ["A", "B"]))
        self.assertEqual(cmap.get(1), "A")
        self.assertEqual(cmap.get(2), "B")
        self.assertEqual(cmap.get(3, ""), "")

    def test_bfchar_and_bfrange_combined(self):
        data = tounicode_bfchar([(1, "甲")]) + b"\n" + tounicode_bfrange(16, 17, 0x4E2D)
        cmap = parse_tounicode(data)
        self.assertEqual(cmap.get(1), "甲")
        self.assertEqual(cmap.get(16), "\u4e2d")
        self.assertEqual(cmap.get(17), "\u4e2e")

    def test_codespace_detects_two_byte_codes(self):
        cmap = parse_tounicode(tounicode_bfchar([(1, "A")]))
        self.assertEqual(cmap.code_bytes, 2)

    def test_codespace_detects_one_byte_codes(self):
        data = (
            b"/CIDInit /ProcSet findresource begin\n"
            b"1 begincodespacerange\n<00> <FF>\nendcodespacerange\n"
            b"1 beginbfchar\n<41> <0041>\nendbfchar\n"
        )
        cmap = parse_tounicode(data)
        self.assertEqual(cmap.code_bytes, 1)
        self.assertEqual(cmap.get(0x41), "A")

    def test_malformed_input_does_not_raise(self):
        # 截断、缺关键字、乱码都不应抛异常
        for junk in (b"beginbfchar", b"<00> <01>", b"\x00\xff" * 20, b"endcmap"):
            parse_tounicode(junk)  # 不抛即可

    def test_unknown_code_returns_default(self):
        cmap = parse_tounicode(tounicode_bfchar([(1, "A")]))
        self.assertEqual(cmap.get(99, "?"), "?")
        self.assertNotIn(99, cmap)


if __name__ == "__main__":
    unittest.main()

"""内容流解释器测试：坐标、字形宽度、文本矩阵推进。"""

from __future__ import annotations

import unittest

from pdfbuilder import one_page_pdf, show, show_cid, simple_font, cid_font, tounicode_bfchar
from pdftext import Extractor, PdfDocument


def chars_of(content: bytes, resources_builder=None, **kwargs):
    """构造单页文档并返回其字符列表。"""
    if resources_builder is None:
        def resources_builder(writer):
            font = simple_font(writer)
            return f"<< /Font << /F1 {font} 0 R >> >>"

    pdf = one_page_pdf(
        lambda w: (content, resources_builder(w)), **kwargs
    )
    page = Extractor(PdfDocument.from_bytes(pdf)).extract_page(0)
    return page.chars


class TestTextPositioning(unittest.TestCase):
    def test_single_tj_advances_by_glyph_width(self):
        """同一个 Tj 里的多个字符必须按字形宽度依次推进。

        宽度 500/1000 × 字号 12 = 6pt，所以 x 应为 100 / 106 / 112。
        """
        content = b"BT /F1 12 Tf 100 700 Td (ABC) Tj ET"
        chars = chars_of(content)
        self.assertEqual([c.text for c in chars], ["A", "B", "C"])
        self.assertAlmostEqual(chars[0].x, 100.0, places=4)
        self.assertAlmostEqual(chars[1].x, 106.0, places=4)
        self.assertAlmostEqual(chars[2].x, 112.0, places=4)
        for char in chars:
            self.assertAlmostEqual(char.adv_x, 6.0, places=4)

    def test_widths_array_is_respected(self):
        """字形宽度不同，推进量也应不同。"""
        def resources_builder(writer):
            # A=1000, B=200, C=500（FirstChar=65）
            font = simple_font(writer, first_char=65, widths=[1000, 200, 500])
            return f"<< /Font << /F1 {font} 0 R >> >>"

        content = b"BT /F1 10 Tf 50 700 Td (ABC) Tj ET"
        chars = chars_of(content, resources_builder)
        # 字号 10：A 前进 10，B 前进 2，C 前进 5
        self.assertAlmostEqual(chars[0].x, 50.0, places=4)
        self.assertAlmostEqual(chars[1].x, 60.0, places=4)
        self.assertAlmostEqual(chars[2].x, 62.0, places=4)
        self.assertAlmostEqual(chars[0].adv_x, 10.0, places=4)
        self.assertAlmostEqual(chars[1].adv_x, 2.0, places=4)

    def test_tj_array_kerning_offsets_position(self):
        """TJ 里的数字是字距调整，单位 1/1000 文本空间。"""
        content = b"BT /F1 12 Tf 100 700 Td [(A) -500 (B) 250 (C)] TJ ET"
        chars = chars_of(content)
        # A@100 前进 6 -> 106；-500 表示 +6 -> 112；B@112 前进 6 -> 118
        # +250 表示 -3 -> 115；C@115
        self.assertEqual([c.text for c in chars], ["A", "B", "C"])
        self.assertAlmostEqual(chars[0].x, 100.0, places=4)
        self.assertAlmostEqual(chars[1].x, 112.0, places=4)
        self.assertAlmostEqual(chars[2].x, 115.0, places=4)

    def test_char_spacing_tc(self):
        content = b"BT /F1 10 Tf 2 Tc 100 700 Td (AB) Tj ET"
        chars = chars_of(content)
        # 前进 = 5 + 2 = 7
        self.assertAlmostEqual(chars[0].x, 100.0, places=4)
        self.assertAlmostEqual(chars[1].x, 107.0, places=4)

    def test_word_spacing_only_applies_to_space(self):
        """字间距 Tw 只作用于单字节空格（码 32）。"""
        content = b"BT /F1 10 Tf 5 Tw 100 700 Td <412041> Tj ET"
        chars = chars_of(content)
        self.assertEqual([c.text for c in chars], ["A", " ", "A"])
        self.assertAlmostEqual(chars[0].x, 100.0, places=4)  # A：前进 5
        self.assertAlmostEqual(chars[1].x, 105.0, places=4)  # 空格：前进 5+5=10
        self.assertAlmostEqual(chars[2].x, 115.0, places=4)

    def test_horizontal_scaling_tz(self):
        content = b"BT /F1 10 Tf 200 Tz 100 700 Td (AB) Tj ET"
        chars = chars_of(content)
        # 前进 = 5 × 2 = 10
        self.assertAlmostEqual(chars[1].x, 110.0, places=4)

    def test_td_accumulates(self):
        """Td 相对**文本行矩阵 Tlm**，而 Tj 只推进 Tm、不动 Tlm。

        所以第二个 Td 从最初的 (100, 700) 起算，而不是从 A 之后的 106 起算。
        这条语义很容易写错，实现和测试都要盯住。
        """
        content = b"BT /F1 12 Tf 100 700 Td (A) Tj 20 0 Td (B) Tj ET"
        chars = chars_of(content)
        self.assertAlmostEqual(chars[0].x, 100.0, places=4)
        self.assertAlmostEqual(chars[1].x, 120.0, places=4)  # 100 + 20，不是 106 + 20

    def test_tstar_uses_leading(self):
        content = b"BT /F1 12 Tf 14 TL 100 700 Td (A) Tj T* (B) Tj ET"
        chars = chars_of(content)
        self.assertAlmostEqual(chars[0].y, 700.0, places=4)
        self.assertAlmostEqual(chars[1].y, 686.0, places=4)

    def test_td_operator_sets_leading(self):
        content = b"BT /F1 12 Tf 100 700 Td (A) Tj 0 -20 TD (B) Tj T* (C) Tj ET"
        chars = chars_of(content)
        self.assertAlmostEqual(chars[1].y, 680.0, places=4)
        # TD 把 leading 设为 20，T* 再往下 20
        self.assertAlmostEqual(chars[2].y, 660.0, places=4)

    def test_quote_operator_moves_to_next_line(self):
        content = b"BT /F1 12 Tf 14 TL 100 700 Td (A) Tj (B) ' ET"
        chars = chars_of(content)
        self.assertAlmostEqual(chars[1].y, 686.0, places=4)

    def test_rotated_text_matrix(self):
        """Tm 里带旋转时，前进体现在 y 方向而不是 x。"""
        content = b"BT /F1 10 Tf 0 1 -1 0 100 700 Tm (AB) Tj ET"
        chars = chars_of(content)
        self.assertAlmostEqual(chars[0].x, 100.0, places=4)
        self.assertAlmostEqual(chars[0].y, 700.0, places=4)
        self.assertAlmostEqual(chars[0].adv_x, 0.0, places=4)
        self.assertAlmostEqual(chars[0].adv_y, 5.0, places=4)
        self.assertAlmostEqual(chars[0].size, 10.0, places=4)


class TestGraphicsState(unittest.TestCase):
    def test_qQ_restores_ctm(self):
        """回归测试：Q 必须恢复 q 保存的 CTM。

        只压不弹会让 CTM 不断复合，整页坐标塌缩（本项目早期真实 bug）。
        """
        content = (
            b"q 1 0 0 1 0 -100 cm\n"
            + show(72, 600, "A").encode("latin-1")
            + b"Q\n"
            + show(72, 600, "B").encode("latin-1")
        )
        chars = chars_of(content)
        # A 被 cm 下移 100 -> 500；Q 之后 B 必须回到 600
        self.assertAlmostEqual(chars[0].y, 500.0, places=4)
        self.assertAlmostEqual(chars[1].y, 600.0, places=4)

    def test_nested_qQ(self):
        content = (
            b"q 1 0 0 1 0 -50 cm\n"
            b"q 1 0 0 1 0 -50 cm\n"
            + show(72, 600, "A").encode("latin-1")
            + b"Q\n"
            + show(72, 600, "B").encode("latin-1")
            + b"Q\n"
            + show(72, 600, "C").encode("latin-1")
        )
        chars = chars_of(content)
        self.assertAlmostEqual(chars[0].y, 500.0, places=4)  # -50 -50
        self.assertAlmostEqual(chars[1].y, 550.0, places=4)  # 回到一层
        self.assertAlmostEqual(chars[2].y, 600.0, places=4)  # 完全恢复

    def test_ctm_scale(self):
        content = b"q 2 0 0 2 0 0 cm\n" + show(50, 300, "A").encode("latin-1") + b"Q\n"
        chars = chars_of(content)
        self.assertAlmostEqual(chars[0].x, 100.0, places=4)
        self.assertAlmostEqual(chars[0].y, 600.0, places=4)
        self.assertAlmostEqual(chars[0].size, 24.0, places=4)  # 12 × 2


class TestCidFonts(unittest.TestCase):
    def test_cid_text_decoded_via_tounicode(self):
        def resources_builder(writer):
            cmap = tounicode_bfchar([(1, "中"), (2, "文")])
            font = cid_font(writer, cmap)
            return f"<< /Font << /F1 {font} 0 R >> >>"

        content = show_cid(72, 700, [1, 2]).encode("latin-1")
        chars = chars_of(content, resources_builder)
        self.assertEqual([c.text for c in chars], ["中", "文"])

    def test_cid_default_width_used(self):
        def resources_builder(writer):
            cmap = tounicode_bfchar([(1, "中"), (2, "文")])
            font = cid_font(writer, cmap, default_width=500)
            return f"<< /Font << /F1 {font} 0 R >> >>"

        content = show_cid(72, 700, [1, 2], size=10).encode("latin-1")
        chars = chars_of(content, resources_builder)
        # DW=500，字号 10 -> 前进 5
        self.assertAlmostEqual(chars[1].x, 77.0, places=4)

    def test_cid_W_array_widths(self):
        def resources_builder(writer):
            cmap = tounicode_bfchar([(1, "甲"), (2, "乙")])
            # /W [1 [1000 200]] -> 码 1 宽 1000，码 2 宽 200
            font = cid_font(writer, cmap, w_array="/W [1 [1000 200]]")
            return f"<< /Font << /F1 {font} 0 R >> >>"

        content = show_cid(72, 700, [1, 2], size=10).encode("latin-1")
        chars = chars_of(content, resources_builder)
        self.assertAlmostEqual(chars[1].x, 82.0, places=4)  # 72 + 10


class TestRobustness(unittest.TestCase):
    def test_tj_outside_bt_does_not_crash(self):
        content = b"/F1 12 Tf 100 700 Td (A) Tj"
        chars = chars_of(content)
        self.assertEqual([c.text for c in chars], ["A"])

    def test_unknown_operator_is_skipped(self):
        content = b"BT /F1 12 Tf 100 700 Td (A) Tj ET 1 2 3 zzzz 4 5 qq"
        chars = chars_of(content)
        self.assertEqual([c.text for c in chars], ["A"])

    def test_undefined_font_yields_no_crash(self):
        content = b"BT /Missing 12 Tf 100 700 Td (A) Tj ET"
        chars = chars_of(content)
        # 字体缺失时不应崩溃（可能提不出文字，但流程要正常）
        self.assertIsInstance(chars, list)

    def test_inline_image_is_skipped(self):
        """内联图像的二进制体里可能含 'EI'，扫描要不被带偏。"""
        content = (
            b"q BI /W 2 /H 1 /CS /G /BPC 8 ID \x01EI\x02 EI Q\n"
            + show(72, 700, "AFTER").encode("latin-1")
        )
        chars = chars_of(content)
        self.assertEqual("".join(c.text for c in chars), "AFTER")

    def test_form_xobject_is_expanded(self):
        """正文常放在 Form XObject 里，不支持就一个字都提不出来。"""
        def setup(writer):
            font = simple_font(writer)
            form_content = show(100, 700, "INFORM", size=12).encode("latin-1")
            form_num = writer.add_stream(
                form_content,
                extra=f"/Type /XObject /Subtype /Form /BBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {font} 0 R >> >>",
            )
            resources = (
                f"<< /Font << /F1 {font} 0 R >> "
                f"/XObject << /Fm1 {form_num} 0 R >> >>"
            )
            return b"q /Fm1 Do Q\n", resources

        page = Extractor(PdfDocument.from_bytes(one_page_pdf(setup))).extract_page(0)
        self.assertEqual(page.text, "INFORM")

    def test_form_xobject_matrix_applied(self):
        def setup(writer):
            font = simple_font(writer)
            form_content = show(100, 700, "M", size=12).encode("latin-1")
            form_num = writer.add_stream(
                form_content,
                extra=f"/Type /XObject /Subtype /Form /BBox [0 0 612 792] "
                f"/Matrix [1 0 0 1 10 20] /Resources << /Font << /F1 {font} 0 R >> >>",
            )
            resources = (
                f"<< /Font << /F1 {font} 0 R >> "
                f"/XObject << /Fm1 {form_num} 0 R >> >>"
            )
            return b"q /Fm1 Do Q\n", resources

        page = Extractor(PdfDocument.from_bytes(one_page_pdf(setup))).extract_page(0)
        self.assertEqual(page.chars[0].x, 110.0)
        self.assertEqual(page.chars[0].y, 720.0)

    def test_zero_font_size_does_not_crash(self):
        content = b"BT /F1 0 Tf 100 700 Td (A) Tj ET"
        chars = chars_of(content)
        self.assertIsInstance(chars, list)


if __name__ == "__main__":
    unittest.main()

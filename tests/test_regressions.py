"""针对具体历史缺陷的回归测试。

每个用例都对应一个"曾经真的错过的"实现细节。这些测试的意义不是覆盖率，
而是**把踩过的坑钉死**：以后重构时若又把这条逻辑改坏，测试会立刻报出来。
"""

from __future__ import annotations

import unittest

from pdfbuilder import (
    cid_font,
    one_page_pdf,
    show,
    show_cid,
    simple_font,
    tounicode_bfchar,
    tounicode_bfrange_array,
)
from pdftext import Extractor, PdfDocument


def extract(setup, **kwargs):
    return Extractor(
        PdfDocument.from_bytes(one_page_pdf(setup, **kwargs))
    ).extract_page(0)


class TestGraphicsStateRestore(unittest.TestCase):
    """缺陷 1：``cm`` 只压栈不弹栈，CTM 无限复合，整页坐标塌缩。

    真实表现：一份校历 PDF 的 500pt 页面高度被压缩进 20pt，
    不同行被合并，字符交错成 ``1年9月4日.，9月6日2，9月7本日0``。

    第一条测试就是当年那份文件的最小复现。
    """

    def test_sequential_translated_blocks_do_not_accumulate(self):
        def setup(writer):
            font = simple_font(writer)
            ops = []
            # 模拟排版软件的做法：每块内容单独 q ... cm ... Q
            for index in range(8):
                ty = -index * 40
                ops.append(f"q 1 0 0 1 0 {ty} cm\n")
                ops.append(show(72, 700, f"ROW{index}"))
                ops.append("Q\n")
            return "".join(ops).encode("latin-1"), f"<< /Font << /F1 {font} 0 R >> >>"

        page = extract(setup)
        texts = [line.text for line in page.lines]

        self.assertEqual(len(page.lines), 8, f"行数应保持 8 行，实际 {len(page.lines)}")
        self.assertEqual(
            texts, [f"ROW{i}" for i in range(8)], "每行文字不得与其他行交错"
        )

        ys = [line.y for line in page.lines]
        self.assertEqual(len(set(round(y, 1) for y in ys)), 8, "8 行的基线必须互不相同")
        self.assertEqual(ys, sorted(ys, reverse=True), "行序应从上到下")
        # 关键：相邻行间距必须保持在 40pt 左右，而不是被压扁
        for upper, lower in zip(ys, ys[1:]):
            self.assertAlmostEqual(upper - lower, 40.0, places=3)

    def test_qQ_restore_after_translation(self):
        def setup(writer):
            font = simple_font(writer)
            content = (
                b"q 1 0 0 1 0 -100 cm\n"
                + show(72, 600, "INSIDE").encode("latin-1")
                + b"Q\n"
                + show(72, 600, "OUTSIDE").encode("latin-1")
            )
            return content, f"<< /Font << /F1 {font} 0 R >> >>"

        page = extract(setup)
        self.assertAlmostEqual(page.lines[0].y, 600.0, places=3)  # OUTSIDE 在上
        self.assertAlmostEqual(page.lines[1].y, 500.0, places=3)  # INSIDE 在下


class TestBfrangeArrayForm(unittest.TestCase):
    """缺陷 2：``bfrange`` 只认递增写法，漏掉数组写法。

    ``<0003> <0005> [<0020> <0021> <0022>]`` 这种写法在中文 PDF 里很常见，
    漏掉就是整段文字变空白或乱码。
    """

    def test_array_form_end_to_end(self):
        def setup(writer):
            cmap = tounicode_bfrange_array(1, 6, ["校", "历", "安", "排", "通", "知"])
            font = cid_font(writer, cmap)
            content = show_cid(72, 700, [1, 2, 3, 4, 5, 6]).encode("latin-1")
            return content, f"<< /Font << /F1 {font} 0 R >> >>"

        self.assertEqual(extract(setup).text, "校历安排通知")

    def test_all_codes_inside_range_mapped(self):
        def setup(writer):
            cmap = tounicode_bfrange_array(10, 13, ["一", "二", "三", "四"])
            font = cid_font(writer, cmap)
            content = show_cid(72, 700, [10, 11, 12, 13]).encode("latin-1")
            return content, f"<< /Font << /F1 {font} 0 R >> >>"

        self.assertEqual(extract(setup).text, "一二三四")


class TestPerFontCMapIsolation(unittest.TestCase):
    """缺陷 3：把多个字体的 ToUnicode 合并成一张全局映射表。

    两个子集字体的码位空间是**独立**的——同一个码 1 在 A 字体里是"甲"，
    在 B 字体里可能是"乙"。合并成一张表后必然有一半解错。
    """

    def test_same_code_different_meaning_per_font(self):
        def setup(writer):
            font_a = cid_font(writer, tounicode_bfchar([(1, "甲"), (2, "丙")]))
            font_b = cid_font(writer, tounicode_bfchar([(1, "乙"), (2, "丁")]))
            resources = f"<< /Font << /FA {font_a} 0 R /FB {font_b} 0 R >> >>"
            content = (
                show_cid(72, 700, [1, 2], font="FA")
                + show_cid(120, 700, [1, 2], font="FB")
            ).encode("latin-1")
            return content, resources

        self.assertEqual(extract(setup).text, "甲丙 乙丁")

    def test_font_switch_within_line(self):
        def setup(writer):
            font_a = cid_font(writer, tounicode_bfchar([(5, "左")]))
            font_b = cid_font(writer, tounicode_bfchar([(5, "右")]))
            resources = f"<< /Font << /FA {font_a} 0 R /FB {font_b} 0 R >> >>"
            # 用 Tm 做绝对定位：Td 是相对文本行矩阵的，连续用会累加偏移
            content = (
                "BT /FA 12 Tf 1 0 0 1 72 700 Tm <0005> Tj "
                "/FB 12 Tf 1 0 0 1 84 700 Tm <0005> Tj ET\n"
            ).encode("latin-1")
            return content, resources

        self.assertEqual(extract(setup).text, "左右")


class TestSpaceHeuristic(unittest.TestCase):
    """缺陷 4：用固定点数阈值判断是否插空格。

    ``if x - prev_x > 6: txt += " "`` 在 6pt 左右的小字号中文上会**每个字都插
    空格**，输出变成 ``本 科 老 生 报 到``。
    """

    def test_small_cjk_line_has_no_spaces(self):
        def setup(writer):
            cmap = tounicode_bfrange_array(1, 4, ["本", "科", "学", "生"])
            font = cid_font(writer, cmap)
            # 6pt 中文，逐字定位，字距恰等于字宽
            ops = [
                f"BT /F1 6 Tf {72 + i * 6.0} 700 Td <{code:04X}> Tj ET\n"
                for i, code in enumerate([1, 2, 3, 4])
            ]
            return "".join(ops).encode("latin-1"), f"<< /Font << /F1 {font} 0 R >> >>"

        self.assertEqual(extract(setup).text, "本科学生")

    def test_real_gap_still_produces_space(self):
        def setup(writer):
            font = simple_font(writer)
            content = (
                show(72, 700, "LEFT") + "BT /F1 12 Tf 300 700 Td <5249474854> Tj ET\n"
            ).encode("latin-1")
            return content, f"<< /Font << /F1 {font} 0 R >> >>"

        text = extract(setup).text
        self.assertIn(" ", text)
        self.assertRegex(text, r"LEFT\s+RIGHT")


class TestTjArraySupport(unittest.TestCase):
    """缺陷 5：完全不解析 ``TJ`` 数组。

    绝大多数用 Word / LaTeX / 浏览器导出的 PDF 都走 ``TJ``，
    只认 ``Tj`` 的实现在这些文件上几乎提不出字。
    """

    def test_tj_array_produces_text(self):
        def setup(writer):
            font = simple_font(writer)
            content = b"BT /F1 12 Tf 72 700 Td [(He) -120 (llo) 60 ( World)] TJ ET"
            return content, f"<< /Font << /F1 {font} 0 R >> >>"

        self.assertEqual(extract(setup).text, "Hello World")

    def test_tj_kerning_does_not_create_spaces(self):
        """小的字距调整不该被误判成空格。"""
        def setup(writer):
            font = simple_font(writer)
            content = b"BT /F1 12 Tf 72 700 Td [(AB) -20 (CD) -15 (EF)] TJ ET"
            return content, f"<< /Font << /F1 {font} 0 R >> >>"

        self.assertEqual(extract(setup).text, "ABCDEF")


class TestGlyphWidthAdvance(unittest.TestCase):
    """缺陷 6：不按字形宽度推进文本矩阵。

    同一个 ``Tj`` 里的多个字符会全部叠在同一个坐标上。
    """

    def test_multi_char_tj_positions_increase(self):
        def setup(writer):
            font = simple_font(writer, first_char=65, widths=[1000, 1000, 1000])
            content = b"BT /F1 10 Tf 100 700 Td (ABC) Tj ET"
            return content, f"<< /Font << /F1 {font} 0 R >> >>"

        page = extract(setup)
        xs = [round(c.x, 3) for c in page.chars]
        self.assertEqual(xs, [100.0, 110.0, 120.0])
        self.assertEqual(page.text, "ABC")

    def test_wide_and_narrow_glyphs_differ(self):
        def setup(writer):
            # 'i' 窄、'W' 宽
            font = simple_font(writer, first_char=65, widths=[200, 1000, 400])
            content = b"BT /F1 10 Tf 0 700 Td (ABC) Tj ET"
            return content, f"<< /Font << /F1 {font} 0 R >> >>"

        page = extract(setup)
        xs = [round(c.x, 3) for c in page.chars]
        self.assertEqual(xs, [0.0, 2.0, 12.0])


class TestXrefRobustness(unittest.TestCase):
    """缺陷 7：正则扫 ``N G obj`` 找对象，不支持现代 PDF 结构。"""

    def test_modern_xref_stream_and_compressed_content(self):
        def setup(writer):
            font = simple_font(writer)
            content = show(72, 700, "Modern").encode("latin-1")
            return content, f"<< /Font << /F1 {font} 0 R >> >>"

        page = Extractor(
            PdfDocument.from_bytes(one_page_pdf(setup, compress=True, xref_stream=True))
        ).extract_page(0)
        self.assertEqual(page.text, "Modern")


if __name__ == "__main__":
    unittest.main()

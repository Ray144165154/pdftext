"""布局重构测试：行聚类与空格判定。"""

from __future__ import annotations

import unittest

from pdftext.content import TextChar
from pdftext.layout import build_layout, render_line


def ch(
    x: float,
    text: str,
    size: float = 12.0,
    adv: float | None = None,
    y: float = 700.0,
    seq: int = 0,
) -> TextChar:
    return TextChar(
        code=ord(text[0]) if text else 0,
        text=text,
        x=x,
        y=y,
        size=size,
        adv_x=adv if adv is not None else size * 0.5,
        adv_y=0.0,
        seq=seq,
    )


class TestRenderLine(unittest.TestCase):
    def test_no_space_at_natural_advance(self):
        chars = [ch(100, "A"), ch(106, "B")]
        self.assertEqual(render_line(chars), "AB")

    def test_space_inserted_on_real_gap(self):
        chars = [ch(100, "A"), ch(130, "B")]
        self.assertEqual(render_line(chars), "A B")

    def test_large_gap_still_single_space(self):
        """间距再大也只补一个空格——多空格是版面模式的职责。"""
        chars = [ch(100, "A"), ch(400, "B")]
        self.assertEqual(render_line(chars), "A B")

    def test_space_scale_follows_font_size(self):
        """间距阈值必须相对字号，否则小字号处会误插空格。"""
        # 字号 24、前进 12、实际间隔 24：属于"正常排布"，不该插空格？
        # 这里间隔 0（紧邻），显然不插
        chars = [ch(100, "A", size=24), ch(112, "B", size=24)]
        self.assertEqual(render_line(chars), "AB")

    def test_small_cjk_not_split(self):
        """回归测试：固定 6pt 阈值会把小字号中文拆成"本 科 老 生"。"""
        chars = [
            ch(72, "本", size=6.0, adv=6.0, seq=0),
            ch(78, "科", size=6.0, adv=6.0, seq=1),
            ch(84, "学", size=6.0, adv=6.0, seq=2),
            ch(90, "生", size=6.0, adv=6.0, seq=3),
        ]
        self.assertEqual(render_line(chars), "本科学生")

    def test_small_cjk_gap_still_detected(self):
        # 同样的 6pt 字号，但间隔拉开到一个字宽，应插空格
        chars = [
            ch(72, "本", size=6.0, adv=6.0, seq=0),
            ch(90, "科", size=6.0, adv=6.0, seq=1),
        ]
        self.assertEqual(render_line(chars), "本 科")

    def test_chars_sorted_by_x(self):
        chars = [ch(112, "C", seq=2), ch(100, "A", seq=0), ch(106, "B", seq=1)]
        self.assertEqual(render_line(chars), "ABC")

    def test_overprinted_duplicate_dropped(self):
        """同位置同字的重复绘制（伪粗体）应去重。"""
        chars = [ch(100, "A", seq=0), ch(100, "A", seq=1), ch(106, "B", seq=2)]
        self.assertEqual(render_line(chars), "AB")

    def test_same_position_different_text_kept(self):
        chars = [ch(100, "A", seq=0), ch(100, "B", seq=1)]
        self.assertIn("A", render_line(chars))
        self.assertIn("B", render_line(chars))

    def test_missing_advance_falls_back_to_size(self):
        # adv_x = 0 —— 宽度缺失时的兜底，不应产生一堆空格
        chars = [ch(100, "A", size=10, adv=0.0), ch(105, "B", size=10, adv=0.0)]
        self.assertEqual(render_line(chars), "AB")

    def test_empty(self):
        self.assertEqual(render_line([]), "")

    def test_trailing_spaces_stripped(self):
        self.assertEqual(render_line([ch(100, "A"), ch(130, "B")]).rstrip(), "A B")


class TestBuildLayout(unittest.TestCase):
    def test_rows_grouped_by_baseline(self):
        chars = [
            ch(100, "A", y=700),
            ch(106, "B", y=700),
            ch(100, "C", y=680),
        ]
        layout = build_layout(chars)
        self.assertEqual(len(layout.lines), 2)
        self.assertEqual(layout.lines[0].text, "AB")
        self.assertEqual(layout.lines[1].text, "C")

    def test_lines_ordered_top_to_bottom(self):
        chars = [ch(100, "LOW", y=100), ch(100, "HIGH", y=700)]
        layout = build_layout(chars)
        self.assertEqual(layout.lines[0].text, "HIGH")
        self.assertEqual(layout.lines[1].text, "LOW")

    def test_slight_baseline_drift_merges(self):
        """同一行内基线有微小抖动（混排不同字体常见）应合并。"""
        chars = [ch(100, "A", size=12, y=700.0), ch(106, "B", size=12, y=700.4)]
        self.assertEqual(len(build_layout(chars).lines), 1)

    def test_large_baseline_gap_splits(self):
        chars = [ch(100, "A", size=12, y=700.0), ch(106, "B", size=12, y=690.0)]
        self.assertEqual(len(build_layout(chars).lines), 2)

    def test_empty(self):
        layout = build_layout([])
        self.assertEqual(layout.lines, [])
        self.assertEqual(layout.text, "")

    def test_text_property_joins_lines(self):
        chars = [ch(100, "A", y=700), ch(100, "B", y=680)]
        self.assertEqual(build_layout(chars).text, "A\nB")

    def test_layout_text_preserves_columns(self):
        """等宽近似版式：同一列的字应落在同一横向位置。"""
        chars = [
            ch(100, "一", size=10, y=700),
            ch(200, "甲", size=10, y=700),
            ch(100, "二", size=10, y=680),
            ch(200, "乙", size=10, y=680),
        ]
        lines = build_layout(chars).layout_text().split("\n")
        self.assertEqual(len(lines), 2)
        # "甲" 在同一列
        self.assertEqual(lines[0].index("甲"), lines[1].index("乙"))

    def test_layout_text_has_no_leading_indent(self):
        """正文整体从页边距开始，不应被推出一大段缩进。

        中文是全角，前进量约等于一个 em（= 字号），不是拉丁字符的 0.5 em。
        """
        chars = [
            ch(72, "正", size=10, adv=10.0, y=700),
            ch(82, "文", size=10, adv=10.0, y=700),
        ]
        rendered = build_layout(chars).layout_text()
        self.assertEqual(rendered, "正文")

    def test_layout_text_halfwidth_uses_half_unit(self):
        """半角字符占半格，不该被撑开。"""
        chars = [
            ch(72, "A", size=10, adv=5.0, y=700),
            ch(77, "B", size=10, adv=5.0, y=700),
        ]
        self.assertEqual(build_layout(chars).layout_text(), "AB")

    def test_line_helpers(self):
        chars = [ch(100, "A"), ch(112, "B")]
        line = build_layout(chars).lines[0]
        self.assertAlmostEqual(line.x, 100.0)
        self.assertAlmostEqual(line.end_x, 118.0)
        self.assertAlmostEqual(line.size, 12.0)
        self.assertEqual(line.fonts, {""})


if __name__ == "__main__":
    unittest.main()

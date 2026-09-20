"""布局重构：把散落的字符还原成行与文本。

内容流给我们的是一堆带坐标的字符，没有任何"行"的概念。要还原成可读文本，
必须自己判断：

1. **哪些字符属于同一行** —— 按基线 y 聚类。阈值必须**相对字号**，用固定
   点数会在小字号处把一行拆成多行（这是常见实现的经典 bug）。

2. **字符之间该不该插空格** —— 不能用一个固定的"间隔超过 N 点就插空格"，
   那会把中文拆成 ``本 科 老 生 报 到``。正确做法是拿**前一个字符的实际
   前进量**算出"下一个字符本来该在哪"，再看实际位置偏了多少。

后者是关键差异：字形的前进量已经包含了字间距 ``Tc``、字距调整 ``TJ`` 和
水平缩放 ``Tz``，所以"实际位置 - 预期位置"能精确区分
"排版本来就该有空格"与"只是字距被拉开了"。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .content import TextChar

__all__ = ["Line", "PageLayout", "build_layout", "render_line"]


# 行聚类：基线差小于 max(0.35 × 字号, 1.0pt) 视为同一行
_LINE_TOLERANCE_RATIO = 0.35
_LINE_TOLERANCE_MIN = 1.0

# 插空格：实际位置比预期位置远出 max(0.28 × 字号, 0.6pt) 才插
_SPACE_RATIO = 0.28
_SPACE_MIN = 0.6


@dataclass(slots=True)
class Line:
    """一行文本。"""

    y: float
    chars: list[TextChar] = field(default_factory=list)
    text: str = ""

    @property
    def x(self) -> float:
        return min((c.x for c in self.chars), default=0.0)

    @property
    def end_x(self) -> float:
        return max((c.end_x for c in self.chars), default=0.0)

    @property
    def size(self) -> float:
        """行内字号的代表值（取中位数，避免个别大字拉偏）。"""
        if not self.chars:
            return 0.0
        sizes = sorted(c.size for c in self.chars)
        return sizes[len(sizes) // 2]

    @property
    def fonts(self) -> set[str]:
        return {c.font for c in self.chars}

    def __str__(self) -> str:
        return self.text


@dataclass(slots=True)
class PageLayout:
    """一页的版面结构。"""

    index: int
    width: float
    height: float
    chars: list[TextChar]
    lines: list[Line]

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    def layout_text(self) -> str:
        """等宽近似版式：按 x 坐标铺到字符网格上，保留列对齐。

        对表格类 PDF（课表、校历、财务报表）很有用，普通正文则不如
        :attr:`text` 干净。
        """
        if not self.lines:
            return ""

        # 用整页字号中位数估一个字符宽度
        all_sizes = sorted(c.size for c in self.chars) or [10.0]
        unit = max(1.0, all_sizes[len(all_sizes) // 2] * 0.5)

        # 以最左侧的内容为第 0 列，避免正文整体被页边距推出一大段缩进
        left_margin = min((c.x for c in self.chars), default=0.0)

        # 先算出每行的字符网格，再统一算最大列数
        grids: list[list[str]] = []
        width_cols = 0
        for line in self.lines:
            row: list[str] = []
            col = 0
            prev: TextChar | None = None
            for ch in line.chars:
                target = int(round((ch.x - left_margin) / unit))
                if prev is not None and target <= col:
                    target = col
                while col < target:
                    row.append(" ")
                    col += 1
                row.append(ch.text)
                span = max(1, int(round(abs(ch.adv_x) / unit)))
                col += span
                prev = ch
            width_cols = max(width_cols, col)
            grids.append(row)

        out: list[str] = []
        for row in grids:
            text = "".join(row).rstrip()
            out.append(text)
        return "\n".join(out)


def _expected_advance(char: TextChar) -> float:
    """字符在设备空间里的前进量；缺失时按字号估一个。"""
    adv = abs(char.adv_x)
    if adv > 0.01:
        return adv
    return max(0.5, char.size * 0.5)


def render_line(chars: list[TextChar]) -> str:
    """把一行里的字符拼成字符串，按需插入空格。"""
    if not chars:
        return ""

    # 按 x 排序；x 相同则按内容流出现顺序（保证稳定）
    ordered = sorted(chars, key=lambda c: (round(c.x, 2), c.seq))

    parts: list[str] = []
    prev: TextChar | None = None
    for ch in ordered:
        if prev is not None:
            # 完全重叠且同字：多为"伪粗体"重复绘制，去重
            if abs(ch.x - prev.x) < 0.05 and ch.text == prev.text and ch.size == prev.size:
                continue

            expected = prev.x + _expected_advance(prev)
            gap = ch.x - expected
            tolerance = max(_SPACE_MIN, _SPACE_RATIO * max(prev.size, ch.size))
            if gap > tolerance:
                # 只补一个空格。纯文本要的是可读性，间距拉开 2 倍还是 20 倍
                # 都只对应"这里有个空白"。需要按坐标精确还原列位置时，
                # 请用 PageLayout.layout_text() / CLI 的 --layout。
                parts.append(" ")
        parts.append(ch.text)
        prev = ch

    return "".join(parts).rstrip()


def build_layout(
    chars: list[TextChar],
    index: int = 0,
    width: float = 0.0,
    height: float = 0.0,
) -> PageLayout:
    """把字符列表聚类成行，产出 :class:`PageLayout`。"""
    if not chars:
        return PageLayout(index=index, width=width, height=height, chars=[], lines=[])

    # 从上到下、从左到右扫描
    ordered = sorted(chars, key=lambda c: (-c.y, c.x, c.seq))

    line_anchors: list[float] = []
    line_sizes: list[float] = []
    buckets: list[list[TextChar]] = []

    for ch in ordered:
        placed = False
        # 只需检查最近几行：字符已按 y 降序排列
        for i in range(len(buckets) - 1, max(-1, len(buckets) - 12), -1):
            tolerance = max(
                _LINE_TOLERANCE_MIN,
                _LINE_TOLERANCE_RATIO * max(line_sizes[i], ch.size),
            )
            if abs(line_anchors[i] - ch.y) <= tolerance:
                buckets[i].append(ch)
                placed = True
                break
        if not placed:
            buckets.append([ch])
            line_anchors.append(ch.y)
            line_sizes.append(ch.size)

    lines: list[Line] = []
    for bucket, anchor in zip(buckets, line_anchors):
        lines.append(Line(y=anchor, chars=bucket, text=render_line(bucket)))

    lines.sort(key=lambda ln: -ln.y)
    return PageLayout(
        index=index, width=width, height=height, chars=chars, lines=lines
    )

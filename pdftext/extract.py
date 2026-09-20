"""顶层提取 API。

把"文档 → 页面 → 内容流 → 字符 → 行 → 文本"这条链路串起来，
对外只暴露几个简单函数。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from .content import IDENTITY, ContentInterpreter, TextChar, mat_mul
from .document import PdfDocument
from .layout import Line, PageLayout, build_layout

__all__ = [
    "Page",
    "Extractor",
    "extract_pages",
    "extract_text",
    "open_pdf",
]


@dataclass(slots=True)
class Page:
    """一页的提取结果。"""

    index: int          # 从 0 开始
    width: float
    height: float
    chars: list[TextChar]
    lines: list[Line]
    rotation: int = 0
    has_text: bool = True

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @property
    def number(self) -> int:
        """页码（从 1 开始），便于面向用户展示。"""
        return self.index + 1

    def layout_text(self) -> str:
        layout = PageLayout(
            index=self.index,
            width=self.width,
            height=self.height,
            chars=self.chars,
            lines=self.lines,
        )
        return layout.layout_text()

    def __str__(self) -> str:
        return self.text


def open_pdf(path) -> PdfDocument:
    """打开 PDF 文件（或字节流）。"""
    if isinstance(path, (bytes, bytearray)):
        return PdfDocument.from_bytes(bytes(path))
    return PdfDocument.from_file(path)


def _media_box(doc: PdfDocument, page) -> tuple[float, float, float, float]:
    box = doc.resolve(page.get("MediaBox"))
    if not isinstance(box, (list, tuple)) or len(box) < 4:
        return (0.0, 0.0, 612.0, 792.0)  # 美式 Letter 兜底
    try:
        x0, y0, x1, y1 = (float(v) for v in box[:4])
    except (TypeError, ValueError):
        return (0.0, 0.0, 612.0, 792.0)
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    return (x0, y0, x1, y1)


def page_geometry(doc: PdfDocument, page) -> tuple[float, float, int, tuple]:
    """返回 ``(显示宽, 显示高, 旋转角, 初始 CTM)``。

    ``/Rotate`` 会让页面的视觉朝向与坐标轴不一致，必须在进入内容流之前
    把旋转并进初始 CTM，否则文字的行序会整体错乱。
    """
    x0, y0, x1, y1 = _media_box(doc, page)
    w, h = x1 - x0, y1 - y0

    rotate = doc.resolve(page.get("Rotate"))
    try:
        rot = int(rotate) % 360 if rotate is not None else 0
    except (TypeError, ValueError):
        rot = 0
    if rot not in (0, 90, 180, 270):
        rot = 0

    # 先把 MediaBox 左下角挪到原点
    normalize = (1.0, 0.0, 0.0, 1.0, -x0, -y0)

    if rot == 90:
        # 顺时针 90°：页宽变为原高
        rotate_m = (0.0, 1.0, -1.0, 0.0, h, 0.0)
        display_w, display_h = h, w
    elif rot == 180:
        rotate_m = (-1.0, 0.0, 0.0, -1.0, w, h)
        display_w, display_h = w, h
    elif rot == 270:
        rotate_m = (0.0, -1.0, 1.0, 0.0, 0.0, w)
        display_w, display_h = h, w
    else:
        rotate_m = IDENTITY
        display_w, display_h = w, h

    return (display_w, display_h, rot, mat_mul(normalize, rotate_m))


def _parse_page_spec(spec: str | None, total: int) -> list[int]:
    """解析 ``"1-3,5,8-"`` 形式的页码范围，返回 0 基索引列表。"""
    if not spec:
        return list(range(total))

    picked: list[int] = []
    for chunk in spec.replace(" ", "").split(","):
        if not chunk:
            continue
        if "-" in chunk:
            lo_s, _, hi_s = chunk.partition("-")
            try:
                lo = int(lo_s) if lo_s else 1
                hi = int(hi_s) if hi_s else total
            except ValueError:
                continue
            if lo > hi:
                lo, hi = hi, lo
            picked.extend(range(max(1, lo), min(total, hi) + 1))
        else:
            try:
                num = int(chunk)
            except ValueError:
                continue
            if 1 <= num <= total:
                picked.append(num)

    # 去重并保持原顺序
    seen: set[int] = set()
    out: list[int] = []
    for num in picked:
        if num not in seen:
            seen.add(num)
            out.append(num)
    return [n - 1 for n in out]


class Extractor:
    """从 :class:`~pdftext.document.PdfDocument` 提取文字。"""

    def __init__(self, doc: PdfDocument) -> None:
        self.doc = doc

    @classmethod
    def from_file(cls, path) -> Extractor:
        return cls(open_pdf(path))

    def page_count(self) -> int:
        return self.doc.page_count

    def extract_page(self, index: int) -> Page:
        """提取第 ``index`` 页（0 基）。"""
        pages = self.doc.pages
        if not (0 <= index < len(pages)):
            raise IndexError(f"页码 {index} 超出范围（共 {len(pages)} 页）")

        page = pages[index]
        width, height, rotation, ctm = page_geometry(self.doc, page)

        resources = self.doc.resolve(page.get("Resources"))
        data = self.doc.page_content(page)

        interpreter = ContentInterpreter(self.doc, index)
        chars = interpreter.run(data, resources, ctm) if data else []

        layout = build_layout(chars, index=index, width=width, height=height)
        return Page(
            index=index,
            width=width,
            height=height,
            chars=chars,
            lines=layout.lines,
            rotation=rotation,
            has_text=bool(chars),
        )

    def extract(self, pages: str | None = None) -> list[Page]:
        """按页码规格提取多页。"""
        total = self.doc.page_count
        indices = _parse_page_spec(pages, total)
        return [self.extract_page(i) for i in indices]

    def iter_pages(self, pages: str | None = None) -> Iterator[Page]:
        yield from self.extract(pages)


def extract_pages(path, pages: str | None = None) -> list[Page]:
    """便捷函数：打开文件并提取若干页。"""
    return Extractor.from_file(path).extract(pages)


def extract_text(path, pages: str | None = None, join: str = "\n\f") -> str:
    """便捷函数：直接拿到整篇纯文本。"""
    return join.join(page.text for page in extract_pages(path, pages))

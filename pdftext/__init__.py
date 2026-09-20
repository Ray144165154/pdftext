"""pdftext —— 零依赖的纯 Python PDF 文本提取器。

只用标准库实现 PDF 解析与文本抽取，不需要 ``pip install`` 任何东西::

    from pdftext import extract_text

    print(extract_text("文档.pdf"))

命令行用法::

    python -m pdftext 文档.pdf -p 1-3 --layout

支持的 PDF 特性见 README；已知不支持项（加密、OCR、竖排）见
``docs/LIMITATIONS.md``。
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = [
    "__version__",
    "PdfDocument",
    "DocumentError",
    "Extractor",
    "Page",
    "extract_pages",
    "extract_text",
    "open_pdf",
    "TextChar",
    "Line",
    "ToUnicodeCMap",
    "parse_tounicode",
]

from .cmap import ToUnicodeCMap, parse_tounicode
from .content import TextChar
from .document import DocumentError, PdfDocument
from .extract import Extractor, Page, extract_pages, extract_text, open_pdf
from .layout import Line

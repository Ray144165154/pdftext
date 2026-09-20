"""纯 Python 的最小 PDF 生成器，用于构造测试夹具。

为什么不直接放几个现成的 PDF 文件进仓库？

  * 二进制样本会随 PDF 规范演进而过时，也看不清"到底在测什么"
  * 网络上的样本有版权问题
  * 自己生成可以**精确控制**每一个字节，比如"故意把 bfrange 写成数组形式"
    来验证解析器是否支持

因此本模块只用标准库手写 PDF 结构，每个夹具都是可读的代码。
"""

from __future__ import annotations

import zlib

__all__ = [
    "PdfWriter",
    "simple_font",
    "cid_font",
    "tounicode_bfchar",
    "tounicode_bfrange",
    "tounicode_bfrange_array",
    "winansi_hex",
    "show",
    "text_pdf",
    "make_standard_pdf",
]


class PdfWriter:
    """组装 PDF 对象并输出完整文件。"""

    def __init__(self) -> None:
        self.objects: dict[int, bytes] = {}
        self._next = 1

    def reserve(self) -> int:
        num = self._next
        self._next += 1
        return num

    def put(self, num: int, body: bytes) -> None:
        self.objects[num] = body

    def add(self, body: bytes) -> int:
        num = self.reserve()
        self.put(num, body)
        return num

    def add_stream(self, data: bytes, extra: str = "", compress: bool = False) -> int:
        if compress:
            data = zlib.compress(data)
            extra = f"/Filter /FlateDecode {extra}".strip()
        num = self.reserve()
        body = (
            f"<< /Length {len(data)} {extra} >>\nstream\n".encode("latin-1")
            + data
            + b"\nendstream"
        )
        self.put(num, body)
        return num

    # -- 输出 ------------------------------------------------------------
    def build(self, root: int, eol: bytes = b"\n") -> bytes:
        """用传统 xref 表输出。

        ``eol`` 可以指定 xref 段的换行符，用来构造"裸 \\r 换行"这类真实世界
        文件（规范允许 ``\\r`` / ``\\n`` / ``\\r\\n`` 三种，解析器必须都认）。
        """
        return self._assemble(root, use_xref_stream=False, eol=eol)

    def build_xref_stream(self, root: int) -> bytes:
        """用交叉引用流（PDF 1.5+）输出，用于测试该解析路径。"""
        return self._assemble(root, use_xref_stream=True)

    def build_compressed(self, root: int, pack: list[int]) -> bytes:
        """把 ``pack`` 里的对象压进一个对象流（``/Type /ObjStm``）再输出。

        这是 PDF 1.5+ 的常见做法：正文对象在文件里没有自己的 ``N G obj``，
        只能通过交叉引用流的 type-2 条目 + 对象流才能找到。
        不实现这条路径的解析器会认为这些对象"不存在"。
        """
        objstm_num = self.reserve()

        header_parts: list[str] = []
        payload = bytearray()
        for num in pack:
            header_parts.append(f"{num} {len(payload)}")
            payload += self.objects[num] + b" "
        header = (" ".join(header_parts) + " ").encode("latin-1")
        first = len(header)
        body = header + bytes(payload)

        compressed = zlib.compress(body)
        self.objects[objstm_num] = (
            f"<< /Type /ObjStm /N {len(pack)} /First {first} "
            f"/Length {len(compressed)} /Filter /FlateDecode >>\nstream\n"
        ).encode("latin-1") + compressed + b"\nendstream"

        packed = {num: idx for idx, num in enumerate(pack)}
        return self._assemble(
            root, use_xref_stream=True, packed=packed, objstm_num=objstm_num
        )

    def _assemble(
        self,
        root: int,
        use_xref_stream: bool,
        packed: dict[int, int] | None = None,
        objstm_num: int | None = None,
        eol: bytes = b"\n",
    ) -> bytes:
        packed = packed or {}
        out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        offsets: dict[int, int] = {}

        for num in sorted(self.objects):
            if num in packed:
                continue  # 成员对象藏在对象流里，文件里没有独立实体
            offsets[num] = len(out)
            out += f"{num} 0 obj\n".encode("latin-1")
            out += self.objects[num]
            out += b"\nendobj\n"

        max_num = max(self.objects) if self.objects else 0

        if not use_xref_stream:
            xref_pos = len(out)
            out += b"xref" + eol
            out += f"0 {max_num + 1} ".encode("latin-1") + eol
            out += b"0000000000 65535 f " + eol
            for num in range(1, max_num + 1):
                if num in offsets:
                    out += f"{offsets[num]:010d} 00000 n ".encode("latin-1") + eol
                else:
                    out += b"0000000000 65535 f " + eol
            out += b"trailer" + eol
            out += f"<< /Size {max_num + 1} /Root {root} 0 R >>".encode("latin-1") + eol
            out += b"startxref" + eol
            out += str(xref_pos).encode("latin-1") + eol
            out += b"%%EOF" + eol
            return bytes(out)

        # 交叉引用流：/W [1 4 2]，每条 7 字节
        xref_num = self.reserve()
        xref_pos = len(out)
        size = max(max_num, xref_num) + 1

        def entry(typ: int, field1: int, field2: int) -> bytes:
            return (
                bytes([typ])
                + (field1 & 0xFFFFFFFF).to_bytes(4, "big")
                + (field2 & 0xFFFF).to_bytes(2, "big")
            )

        table = bytearray()
        for num in range(size):
            if num == 0:
                table += entry(0, 0, 65535)
            elif num == xref_num:
                table += entry(1, xref_pos, 0)
            elif num in packed and objstm_num is not None:
                table += entry(2, objstm_num, packed[num])
            elif num in offsets:
                table += entry(1, offsets[num], 0)
            else:
                table += entry(0, 0, 65535)

        compressed = zlib.compress(bytes(table))
        out += f"{xref_num} 0 obj\n".encode("latin-1")
        out += (
            f"<< /Type /XRef /Size {size} /W [1 4 2] /Root {root} 0 R "
            f"/Length {len(compressed)} /Filter /FlateDecode >>\nstream\n"
        ).encode("latin-1")
        out += compressed
        out += b"\nendstream\nendobj\n"
        out += f"startxref\n{xref_pos}\n%%EOF\n".encode("latin-1")
        return bytes(out)


def winansi_hex(text: str) -> str:
    """把文本编成 WinAnsi 的十六进制字符串（内容流里用 <...> 更省心）。"""
    return text.encode("cp1252", "replace").hex().upper()


def show(x: float, y: float, text: str, size: float = 12, font: str = "F1") -> str:
    """生成一段"定位并显示文字"的内容流指令（使用 WinAnsi 单字节编码）。"""
    return f"BT /{font} {size} Tf {x} {y} Td <{winansi_hex(text)}> Tj ET\n"


def show_cid(x: float, y: float, codes: list[int], size: float = 12, font: str = "F1") -> str:
    """生成显示 CID 字体的指令（双字节码）。"""
    hex_str = "".join(f"{c:04X}" for c in codes)
    return f"BT /{font} {size} Tf {x} {y} Td <{hex_str}> Tj ET\n"


# --------------------------------------------------------------------------
# 字体
# --------------------------------------------------------------------------


def simple_font(
    writer: PdfWriter,
    base_font: str = "Helvetica",
    encoding: str = "WinAnsiEncoding",
    first_char: int = 32,
    widths: list[int] | None = None,
) -> int:
    """构造一个简单字体（单字节编码）。

    默认宽度全给 500，测试里只关心定位逻辑，不追求真实字形宽度。
    """
    if widths is None:
        widths = [500] * (127 - first_char)
    widths_str = " ".join(str(w) for w in widths)
    return writer.add(
        (
            f"<< /Type /Font /Subtype /Type1 /BaseFont /{base_font} "
            f"/Encoding /{encoding} /FirstChar {first_char} "
            f"/LastChar {first_char + len(widths) - 1} /Widths [{widths_str}] >>"
        ).encode("latin-1")
    )


def tounicode_bfchar(pairs: list[tuple[int, str]]) -> bytes:
    """生成用 ``beginbfchar`` 写法的 ToUnicode CMap。"""
    lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CMapName /Test-UCS2 def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<0000> <FFFF>",
        "endcodespacerange",
        f"{len(pairs)} beginbfchar",
    ]
    for code, text in pairs:
        payload = text.encode("utf-16-be").hex().upper()
        lines.append(f"<{code:04X}> <{payload}>")
    lines += [
        "endbfchar",
        "endcmap",
        "CMapName currentdict /CMap defineresource pop",
        "end",
        "end",
    ]
    return ("\n".join(lines) + "\n").encode("latin-1")


def tounicode_bfrange(lo: int, hi: int, base: int) -> bytes:
    """生成 ``beginbfrange`` 的**递增**写法：``<lo> <hi> <base>``。"""
    lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CMapName /Test-UCS2 def",
        "1 begincodespacerange",
        "<0000> <FFFF>",
        "endcodespacerange",
        "1 beginbfrange",
        f"<{lo:04X}> <{hi:04X}> <{base:04X}>",
        "endbfrange",
        "endcmap",
        "CMapName currentdict /CMap defineresource pop",
        "end",
        "end",
    ]
    return ("\n".join(lines) + "\n").encode("latin-1")


def tounicode_bfrange_array(lo: int, hi: int, texts: list[str]) -> bytes:
    """生成 ``beginbfrange`` 的**数组**写法：``<lo> <hi> [<d1> <d2> ...]``。

    这就是早期实现漏掉的分支——很多中文 PDF 用它，漏掉会导致整段乱码。
    """
    items = " ".join(
        f"<{text.encode('utf-16-be').hex().upper()}>" for text in texts
    )
    lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CMapName /Test-UCS2 def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<0000> <FFFF>",
        "endcodespacerange",
        "1 beginbfrange",
        f"<{lo:04X}> <{hi:04X}> [{items}]",
        "endbfrange",
        "endcmap",
        "CMapName currentdict /CMap defineresource pop",
        "end",
        "end",
    ]
    return ("\n".join(lines) + "\n").encode("latin-1")


def cid_font(
    writer: PdfWriter,
    tounicode: bytes,
    w_array: str = "",
    default_width: int = 1000,
    base_font: str = "TestCJK",
) -> int:
    """构造 Type0 / Identity-H 复合字体（中文 PDF 的典型结构）。"""
    tounicode_num = writer.add_stream(tounicode)

    cid_font_dict = (
        f"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /{base_font} "
        f"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
        f"/DW {default_width} {w_array} >>"
    )
    cid_num = writer.add(cid_font_dict.encode("latin-1"))

    return writer.add(
        (
            f"<< /Type /Font /Subtype /Type0 /BaseFont /{base_font} "
            f"/Encoding /Identity-H /DescendantFonts [{cid_num} 0 R] "
            f"/ToUnicode {tounicode_num} 0 R >>"
        ).encode("latin-1")
    )


# --------------------------------------------------------------------------
# 高层组装
# --------------------------------------------------------------------------


def text_pdf(
    content: bytes,
    resources: str,
    *,
    compress: bool = False,
    mediabox: tuple[float, float, float, float] = (0.0, 0.0, 612.0, 792.0),
    rotate: int = 0,
    xref_stream: bool = False,
) -> bytes:
    """用给定的内容流和资源字典组装一个单页 PDF。"""
    writer = PdfWriter()
    content_num = writer.add_stream(content, compress=compress)

    page_num = writer.reserve()
    pages_num = writer.reserve()
    root_num = writer.reserve()

    x0, y0, x1, y1 = mediabox
    rotate_str = f" /Rotate {rotate}" if rotate else ""

    writer.put(
        page_num,
        (
            f"<< /Type /Page /Parent {pages_num} 0 R "
            f"/MediaBox [{x0} {y0} {x1} {y1}]{rotate_str} "
            f"/Resources {resources} /Contents {content_num} 0 R >>"
        ).encode("latin-1"),
    )
    writer.put(
        pages_num,
        (
            f"<< /Type /Pages /Kids [{page_num} 0 R] /Count 1 >>"
        ).encode("latin-1"),
    )
    writer.put(
        root_num,
        f"<< /Type /Catalog /Pages {pages_num} 0 R >>".encode("latin-1"),
    )

    return writer.build_xref_stream(root_num) if xref_stream else writer.build(root_num)


def one_page_pdf(
    setup,
    *,
    compress: bool = False,
    mediabox: tuple[float, float, float, float] = (0.0, 0.0, 612.0, 792.0),
    rotate: int = 0,
    xref_stream: bool = False,
) -> bytes:
    """用回调组装单页文档：``setup(writer) -> (内容流字节, 资源字典字符串)``。

    这样字体和内容流可以共用同一个 :class:`PdfWriter`，不必手动传对象号。
    """
    writer = PdfWriter()
    content, resources = setup(writer)
    content_num = writer.add_stream(content, compress=compress)

    page_num = writer.reserve()
    pages_num = writer.reserve()
    root_num = writer.reserve()

    x0, y0, x1, y1 = mediabox
    rotate_str = f" /Rotate {rotate}" if rotate else ""
    writer.put(
        page_num,
        (
            f"<< /Type /Page /Parent {pages_num} 0 R "
            f"/MediaBox [{x0} {y0} {x1} {y1}]{rotate_str} "
            f"/Resources {resources} /Contents {content_num} 0 R >>"
        ).encode("latin-1"),
    )
    writer.put(pages_num, f"<< /Type /Pages /Kids [{page_num} 0 R] /Count 1 >>".encode("latin-1"))
    writer.put(root_num, f"<< /Type /Catalog /Pages {pages_num} 0 R >>".encode("latin-1"))

    return writer.build_xref_stream(root_num) if xref_stream else writer.build(root_num)


def multi_page_pdf(setups, *, compress: bool = False) -> bytes:
    """组装多页文档，用于测试页面树遍历与页码选择。"""
    writer = PdfWriter()
    pages_num = writer.reserve()
    root_num = writer.reserve()

    kids: list[int] = []
    for setup in setups:
        content, resources = setup(writer)
        content_num = writer.add_stream(content, compress=compress)
        page_num = writer.reserve()
        writer.put(
            page_num,
            (
                f"<< /Type /Page /Parent {pages_num} 0 R /MediaBox [0 0 612 792] "
                f"/Resources {resources} /Contents {content_num} 0 R >>"
            ).encode("latin-1"),
        )
        kids.append(page_num)

    kids_str = " ".join(f"{n} 0 R" for n in kids)
    writer.put(
        pages_num,
        f"<< /Type /Pages /Kids [{kids_str}] /Count {len(kids)} >>".encode("latin-1"),
    )
    writer.put(root_num, f"<< /Type /Catalog /Pages {pages_num} 0 R >>".encode("latin-1"))
    return writer.build(root_num)


def make_standard_pdf(
    lines: list[tuple[float, float, str]],
    *,
    compress: bool = False,
    xref_stream: bool = False,
    eol: bytes = b"\n",
) -> bytes:
    """最简夹具：若干行 WinAnsi 文本。"""
    writer = PdfWriter()
    font_num = simple_font(writer)
    content = "".join(show(x, y, text) for x, y, text in lines).encode("latin-1")
    content_num = writer.add_stream(content, compress=compress)

    page_num = writer.reserve()
    pages_num = writer.reserve()
    root_num = writer.reserve()
    writer.put(
        page_num,
        (
            f"<< /Type /Page /Parent {pages_num} 0 R "
            f"/MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_num} 0 R >> >> "
            f"/Contents {content_num} 0 R >>"
        ).encode("latin-1"),
    )
    writer.put(pages_num, f"<< /Type /Pages /Kids [{page_num} 0 R] /Count 1 >>".encode("latin-1"))
    writer.put(root_num, f"<< /Type /Catalog /Pages {pages_num} 0 R >>".encode("latin-1"))

    return writer.build_xref_stream(root_num) if xref_stream else writer.build(root_num, eol=eol)

"""字体处理：编码、字形宽度、ToUnicode 映射。

PDF 里有两族字体，解码方式完全不同：

**简单字体**（Type1 / TrueType / Type3）
    内容流里每个字节就是一个字符码，经 ``/Encoding`` 查表得到字形名，
    再由字形名得到 Unicode。没有 ``/ToUnicode`` 时靠编码表兜底。

**复合字体**（Type0，含 Identity-H）
    内容流里是双字节的 CID/GID。绝大多数中文 PDF 走这条路，**必须**靠
    ``/ToUnicode`` 才能还原文字；没有它就只剩字形编号，神仙难救。

字形宽度同样关键：文本定位要按 ``w0/1000 * 字号`` 推进文本矩阵。**不读宽度
就没法算下一个字符的 x 坐标**，多字符共享一个 ``Tj`` 的 PDF 会全部叠在一起。
宽度来源：

  * 简单字体 ``/FirstChar`` + ``/Widths``，缺省值取 ``/FontDescriptor/MissingWidth``
  * 复合字体 ``/W`` 数组（三种写法）与 ``/DW``
"""

from __future__ import annotations

from typing import Any

from .cmap import ToUnicodeCMap, decode_utf16be, parse_tounicode
from .objects import PdfArray, PdfDict, PdfName, PdfStream

__all__ = ["Font", "build_font", "STANDARD_ENCODINGS"]


# --------------------------------------------------------------------------
# 编码表
# --------------------------------------------------------------------------

def _build_ascii_glyph_names() -> dict[int, str]:
    """按 Adobe 规范生成 ASCII 区（32..126）的字形名。"""
    names: dict[int, str] = {}
    base = (
        "space exclam quotedbl numbersign dollar percent ampersand quotesingle "
        "parenleft parenright asterisk plus comma hyphen period slash "
        "zero one two three four five six seven eight nine "
        "colon semicolon less equal greater question at"
    ).split()
    assert len(base) == 33
    for i, n in enumerate(base):
        names[32 + i] = n
    for i in range(26):
        names[65 + i] = chr(65 + i)
    for i, n in enumerate(
        "bracketleft backslash bracketright asciicircum underscore grave".split()
    ):
        names[91 + i] = n
    for i in range(26):
        names[97 + i] = chr(97 + i)
    for i, n in enumerate("braceleft bar braceright asciitilde".split()):
        names[123 + i] = n
    return names


_ASCII_GLYPH_NAMES = _build_ascii_glyph_names()

# 字形名 -> Unicode。ASCII 部分程序化生成，其余是实际 PDF 里高频出现的补充项。
_GLYPH_NAME_TO_UNICODE: dict[str, str] = {
    name: chr(code) for code, name in _ASCII_GLYPH_NAMES.items()
}
_GLYPH_NAME_TO_UNICODE.update(
    {
        "quoteright": "\u2019", "quoteleft": "\u2018",
        "quotedblleft": "\u201c", "quotedblright": "\u201d",
        "quotesinglbase": "\u201a", "quotedblbase": "\u201e",
        "endash": "\u2013", "emdash": "\u2014", "ellipsis": "\u2026",
        "bullet": "\u2022", "dagger": "\u2020", "daggerdbl": "\u2021",
        "periodcentered": "\u00b7", "perthousand": "\u2030",
        "guillemotleft": "\u00ab", "guillemotright": "\u00bb",
        "guilsinglleft": "\u2039", "guilsinglright": "\u203a",
        "fi": "\ufb01", "fl": "\ufb02", "ff": "\ufb00", "ffi": "\ufb03", "ffl": "\ufb04",
        "fraction": "\u2044", "minus": "\u2212", "degree": "\u00b0",
        "sterling": "\u00a3", "yen": "\u00a5", "cent": "\u00a2",
        "section": "\u00a7", "paragraph": "\u00b6", "copyright": "\u00a9",
        "registered": "\u00ae", "trademark": "\u2122", "Euro": "\u20ac",
        "AE": "\u00c6", "ae": "\u00e6", "OE": "\u0152", "oe": "\u0153",
        "Oslash": "\u00d8", "oslash": "\u00f8", "Lslash": "\u0141", "lslash": "\u0142",
        "germandbls": "\u00df", "dotlessi": "\u0131",
        "space": " ", "nbspace": "\u00a0", "sfthyphen": "\u00ad",
        "arrowleft": "\u2190", "arrowup": "\u2191",
        "arrowright": "\u2192", "arrowdown": "\u2193",
        "arrowboth": "\u2194", "arrowdblleft": "\u21d0", "arrowdblright": "\u21d2",
        "lessequal": "\u2264", "greaterequal": "\u2265", "notequal": "\u2260",
        "infinity": "\u221e", "partialdiff": "\u2202", "summation": "\u2211",
        "product": "\u220f", "radical": "\u221a", "integral": "\u222b",
        "approxequal": "\u2248", "element": "\u2208", "logicalnot": "\u00ac",
        "alpha": "\u03b1", "beta": "\u03b2", "gamma": "\u03b3", "delta": "\u03b4",
        "epsilon": "\u03b5", "theta": "\u03b8", "lambda": "\u03bb", "mu": "\u03bc",
        "pi": "\u03c0", "sigma": "\u03c3", "phi": "\u03c6", "omega": "\u03c9",
        "Delta": "\u0394", "Sigma": "\u03a3", "Omega": "\u03a9", "Pi": "\u03a0",
    }
)


def glyph_name_to_unicode(name: str) -> str:
    """字形名转 Unicode，支持 ``uniXXXX`` / ``uXXXXXX`` 算法命名。"""
    if not name:
        return ""
    if name in _GLYPH_NAME_TO_UNICODE:
        return _GLYPH_NAME_TO_UNICODE[name]

    # uniXXXX：一个或多个 4 位十六进制码位
    if name.startswith("uni") and len(name) >= 7:
        hexpart = name[3:]
        try:
            if len(hexpart) % 4 == 0:
                return "".join(
                    chr(int(hexpart[i : i + 4], 16))
                    for i in range(0, len(hexpart), 4)
                )
        except ValueError:
            pass

    # uXXXX / uXXXXXX
    if name.startswith("u") and len(name) > 4:
        try:
            cp = int(name[1:], 16)
            if 0 < cp <= 0x10FFFF:
                return chr(cp)
        except ValueError:
            pass

    # 单个字母的变体名，如 a.sc / one.oldstyle
    if "." in name:
        return glyph_name_to_unicode(name.split(".", 1)[0])

    # 无法识别：用码位形式兜底，至少不丢字符
    if name.startswith(("g", "cid", "index", "glyph")):
        return ""
    return ""


def _build_winansi() -> dict[int, str]:
    table: dict[int, str] = {}
    for code in range(256):
        try:
            table[code] = bytes([code]).decode("cp1252")
        except UnicodeDecodeError:
            continue
    # 0x80-0x9F 在 PDF 的 WinAnsi 里多数是未定义，cp1252 已给出正确映射
    return table


def _build_macroman() -> dict[int, str]:
    table: dict[int, str] = {}
    for code in range(256):
        try:
            table[code] = bytes([code]).decode("mac_roman")
        except UnicodeDecodeError:
            continue
    return table


# StandardEncoding 的高位区（0xA0-0xFF）手动建表；低位与 ASCII 一致
_STANDARD_HIGH = {
    0xA1: "\u00a1", 0xA2: "\u00a2", 0xA3: "\u00a3", 0xA4: "\u2044",
    0xA5: "\u00a5", 0xA6: "\u0192", 0xA7: "\u00a7", 0xA8: "\u00a4",
    0xA9: "'", 0xAA: "\u201c", 0xAB: "\u00ab", 0xAC: "\u2039",
    0xAD: "\u203a", 0xAE: "\ufb01", 0xAF: "\ufb02",
    0xB1: "\u2013", 0xB2: "\u2020", 0xB3: "\u2021", 0xB4: "\u00b7",
    0xB6: "\u00b6", 0xB7: "\u2022", 0xB8: "\u201a", 0xB9: "\u201e",
    0xBA: "\u201d", 0xBB: "\u00bb", 0xBC: "\u2026", 0xBD: "\u2030",
    0xBF: "\u00bf", 0xC1: "`", 0xC2: "\u00b4", 0xC3: "\u02c6",
    0xC4: "\u02dc", 0xC5: "\u00af", 0xC6: "\u02d8", 0xC7: "\u02d9",
    0xC8: "\u00a8", 0xCA: "\u02da", 0xCB: "\u00b8", 0xCD: "\u02dd",
    0xCE: "\u02db", 0xCF: "\u02c7", 0xD0: "\u2014",
    0xE1: "\u00c6", 0xE3: "\u00aa", 0xE8: "\u0141", 0xE9: "\u00d8",
    0xEA: "\u0152", 0xEB: "\u00ba", 0xF1: "\u00e6", 0xF5: "\u0131",
    0xF8: "\u0142", 0xF9: "\u00f8", 0xFA: "\u0153", 0xFB: "\u00df",
}


def _build_standard() -> dict[int, str]:
    table: dict[int, str] = {code: chr(code) for code in range(32, 127)}
    # StandardEncoding 的两处例外
    table[0x27] = "\u2019"  # quoteright
    table[0x60] = "\u2018"  # quoteleft
    table.update(_STANDARD_HIGH)
    return table


STANDARD_ENCODINGS: dict[str, dict[int, str]] = {
    "WinAnsiEncoding": _build_winansi(),
    "MacRomanEncoding": _build_macroman(),
    "StandardEncoding": _build_standard(),
    "PDFDocEncoding": _build_winansi(),
    "MacExpertEncoding": _build_macroman(),
}


# --------------------------------------------------------------------------
# 宽度提取
# --------------------------------------------------------------------------

def _parse_cid_widths(w_array: Any, default: float) -> dict[int, float]:
    """解析 CID 字体的 ``/W`` 数组。

    三种写法::

        /W [ 120 [400 325 500] ]      # 120 起连续若干宽度
        /W [ 120 180 500 ]            # 120..180 全是 500
        /W [ 120 [400] 200 210 600 ]  # 混用
    """
    widths: dict[int, float] = {}
    if not isinstance(w_array, (list, tuple)):
        return widths

    i = 0
    n = len(w_array)
    while i < n:
        first = w_array[i]
        if not isinstance(first, (int, float)):
            i += 1
            continue
        first = int(first)
        i += 1
        if i >= n:
            break
        second = w_array[i]

        if isinstance(second, (list, tuple)):
            # 形式一：起始 CID + 宽度数组
            for offset, w in enumerate(second):
                if isinstance(w, (int, float)):
                    widths[first + offset] = float(w)
            i += 1
        elif isinstance(second, (int, float)) and i + 1 < n:
            third = w_array[i + 1]
            if isinstance(third, (int, float)):
                last = int(second)
                if last >= first and last - first <= 65536:
                    for cid in range(first, last + 1):
                        widths[cid] = float(third)
                i += 2
            else:
                i += 1
        else:
            i += 1
    return widths


def _parse_simple_widths(font_dict: PdfDict, doc) -> tuple[dict[int, float], float]:
    """解析简单字体的 ``/FirstChar`` + ``/Widths``。"""
    widths: dict[int, float] = {}
    first_char = font_dict.get("FirstChar")
    raw = doc.resolve(font_dict.get("Widths")) if doc is not None else font_dict.get("Widths")

    if isinstance(raw, (list, tuple)) and isinstance(first_char, int):
        for offset, w in enumerate(raw):
            if isinstance(w, (int, float)):
                widths[first_char + offset] = float(w)

    # 缺省宽度来自字体描述符
    default = 0.0
    descriptor = doc.resolve(font_dict.get("FontDescriptor")) if doc is not None else None
    if isinstance(descriptor, PdfDict):
        missing = descriptor.get("MissingWidth")
        if isinstance(missing, (int, float)):
            default = float(missing)

    if not widths and not default:
        default = 500.0  # 规范给的常见缺省值，避免所有字叠在一起
    return widths, default


# --------------------------------------------------------------------------
# Font
# --------------------------------------------------------------------------


class Font:
    """一个已解析好的字体，负责"字节码 -> 文字"和"字节码 -> 宽度"。"""

    __slots__ = (
        "name", "subtype", "base_font", "is_cid", "code_bytes",
        "to_unicode", "widths", "default_width", "encoding_table",
        "differences", "has_tounicode",
    )

    def __init__(
        self,
        name: str = "",
        subtype: str = "",
        base_font: str = "",
        is_cid: bool = False,
        code_bytes: int = 1,
        to_unicode: dict[int, str] | None = None,
        widths: dict[int, float] | None = None,
        default_width: float = 0.0,
        encoding_table: dict[int, str] | None = None,
        differences: dict[int, str] | None = None,
    ) -> None:
        self.name = name
        self.subtype = subtype
        self.base_font = base_font
        self.is_cid = is_cid
        self.code_bytes = code_bytes
        self.to_unicode = to_unicode or {}
        self.widths = widths or {}
        self.default_width = default_width
        self.encoding_table = encoding_table
        self.differences = differences or {}
        self.has_tounicode = bool(self.to_unicode)

    # -- 解码 ------------------------------------------------------------
    def split_codes(self, data: bytes) -> list[int]:
        """把内容流里的字节串切成一个个字符码。

        复合字体是固定双字节；简单字体是单字节。切错会导致后半段全乱码。
        """
        if self.code_bytes == 2:
            if len(data) % 2:
                data = data[:-1]
            return [int.from_bytes(data[i : i + 2], "big") for i in range(0, len(data), 2)]
        return list(data)

    def decode_code(self, code: int) -> str:
        """把单个字符码转成文字。"""
        text = self.to_unicode.get(code)
        if text is not None:
            return text

        if not self.is_cid:
            # /Differences 优先于基础编码表
            if code in self.differences:
                return self.differences[code]
            if self.encoding_table is not None:
                return self.encoding_table.get(code, "")
        return ""

    def decode(self, data: bytes) -> list[tuple[int, str]]:
        """把字节串解码成 ``[(字符码, 文字), ...]``。

        返回码和文字分开，是因为宽度要用**字符码**查，而输出要用文字。
        """
        out: list[tuple[int, str]] = []
        for code in self.split_codes(data):
            out.append((code, self.decode_code(code)))
        return out

    def decode_text(self, data: bytes) -> str:
        return "".join(text for _code, text in self.decode(data))

    # -- 宽度 ------------------------------------------------------------
    def width(self, code: int) -> float:
        """字形宽度，单位是 1/1000 文本空间。"""
        w = self.widths.get(code)
        if w is not None:
            return w
        return self.default_width

    def __repr__(self) -> str:
        kind = "CID" if self.is_cid else "simple"
        return (
            f"<Font {self.name!r} {self.subtype} {kind} "
            f"codes={self.code_bytes}B toUnicode={len(self.to_unicode)}>"
        )


# --------------------------------------------------------------------------
# 构造
# --------------------------------------------------------------------------


def _resolve_encoding(
    doc, font_dict: PdfDict
) -> tuple[dict[int, str] | None, dict[int, str]]:
    """返回 ``(基础编码表, differences 映射)``。"""
    encoding = doc.resolve(font_dict.get("Encoding")) if doc is not None else font_dict.get("Encoding")
    base_name = None
    differences: dict[int, str] = {}

    if isinstance(encoding, PdfName):
        base_name = str(encoding)
    elif isinstance(encoding, PdfDict):
        base = encoding.get("BaseEncoding")
        if isinstance(base, PdfName):
            base_name = str(base)
        diff = doc.resolve(encoding.get("Differences")) if doc is not None else encoding.get("Differences")
        if isinstance(diff, (list, tuple)):
            current = 0
            for item in diff:
                if isinstance(item, (int, float)):
                    current = int(item)
                elif isinstance(item, PdfName):
                    differences[current] = glyph_name_to_unicode(str(item))
                    current += 1

    # Symbol / ZapfDingbats 有自己的一套字形名，这里不做专门处理
    table = STANDARD_ENCODINGS.get(base_name) if base_name else None
    if table is None and not differences:
        table = STANDARD_ENCODINGS["StandardEncoding"]
    return table, differences


def build_font(doc, font_dict: Any, name: str = "") -> Font:
    """从字体字典构造 :class:`Font`。

    ``font_dict`` 可以是 :class:`~pdftext.objects.PdfDict`、``PdfRef``
    或流对象。任何一步失败都不会抛异常，而是降级出一个"尽量能用"的字体。
    """
    font_dict = doc.resolve(font_dict) if doc is not None else font_dict
    if not isinstance(font_dict, PdfDict):
        return Font(name=name, subtype="?", code_bytes=1)

    subtype = str(font_dict.get("Subtype", ""))
    base_font = str(font_dict.get("BaseFont", ""))
    is_cid = subtype == "Type0"

    # ---- ToUnicode --------------------------------------------------
    to_unicode: dict[int, str] = {}
    tounicode_raw = font_dict.get("ToUnicode")
    cmap_obj = None
    if tounicode_raw is not None and doc is not None:
        cmap_obj = doc.resolve(tounicode_raw)
        if isinstance(cmap_obj, PdfStream):
            try:
                cmap = parse_tounicode(doc.get_stream_data(cmap_obj))
                to_unicode = dict(cmap.mapping)
            except Exception:
                to_unicode = {}

    # ---- 宽度与编码 --------------------------------------------------
    if is_cid:
        # 复合字体：宽度和子字体信息都在 DescendantFonts[0]
        descendants = doc.resolve(font_dict.get("DescendantFonts")) if doc is not None else None
        cid_font = None
        if isinstance(descendants, (list, tuple)) and descendants:
            cid_font = doc.resolve(descendants[0])

        default_width = 1000.0
        widths: dict[int, float] = {}
        if isinstance(cid_font, PdfDict):
            dw = cid_font.get("DW")
            if isinstance(dw, (int, float)):
                default_width = float(dw)
            w_arr = doc.resolve(cid_font.get("W")) if doc is not None else cid_font.get("W")
            widths = _parse_cid_widths(w_arr, default_width)

        # 码宽：Identity-H/V 一律双字节；具名 CMap 也按双字节处理
        code_bytes = 2
        return Font(
            name=name,
            subtype=subtype,
            base_font=base_font,
            is_cid=True,
            code_bytes=code_bytes,
            to_unicode=to_unicode,
            widths=widths,
            default_width=default_width,
        )

    # 简单字体
    widths, default_width = _parse_simple_widths(font_dict, doc)
    table, differences = _resolve_encoding(doc, font_dict)

    return Font(
        name=name,
        subtype=subtype,
        base_font=base_font,
        is_cid=False,
        code_bytes=1,
        to_unicode=to_unicode,
        widths=widths,
        default_width=default_width,
        encoding_table=table,
        differences=differences,
    )


def build_fonts(doc, resources: Any) -> dict[str, Font]:
    """从页面的 ``/Resources`` 构造 ``{资源名: Font}``。"""
    fonts: dict[str, Font] = {}
    resources = doc.resolve(resources) if doc is not None else resources
    if not isinstance(resources, PdfDict):
        return fonts

    font_dict = doc.resolve(resources.get("Font")) if doc is not None else resources.get("Font")
    if not isinstance(font_dict, PdfDict):
        return fonts

    for key, value in font_dict.items():
        try:
            fonts[str(key)] = build_font(doc, value, str(key))
        except Exception:
            fonts[str(key)] = Font(name=str(key), subtype="?")
    return fonts

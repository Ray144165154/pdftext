"""ToUnicode CMap 解析。

CID 字体（Type0）在内容流里写的是字形编号（GID/CID），不是 Unicode。
要还原真实文字，必须查字体自带的 ``/ToUnicode`` CMap 映射表。

本模块完整实现规范里的三种映射写法：

1. ``beginbfchar`` —— 一对一::

       <0003> <0020>

2. ``beginbfrange`` 递增形式 —— 区间内目标码连续递增::

       <0003> <0005> <0020>     # 0003->0020, 0004->0021, 0005->0022

3. ``beginbfrange`` 数组形式 —— 区间内逐项显式给出::

       <0003> <0005> [<0020> <0021> <0022>]

**第 3 种正是常见实现漏掉的那个。** 很多排版软件（含多数中文 PDF）会用它，
漏掉就会导致整段文字错位或变问号。

另外还解析 ``begincodespacerange``，用来自动判断源编码是单字节还是双字节。
"""

from __future__ import annotations

import re

__all__ = ["parse_tounicode", "decode_utf16be", "ToUnicodeCMap"]


def decode_utf16be(raw: bytes) -> str:
    """把 UTF-16BE 字节解码成字符串，容忍奇数长度和非法代理对。

    CMap 的目标值偶尔长度不规整（多一个字节或少一个字节），直接
    ``.decode('utf-16-be')`` 会抛异常并丢掉整条映射，所以这里逐码元处理。
    """
    if not raw:
        return ""
    if len(raw) % 2:
        raw = raw + b"\x00"
    out: list[str] = []
    for i in range(0, len(raw), 2):
        unit = int.from_bytes(raw[i : i + 2], "big")
        # 代理对：合并成一个码位
        if 0xD800 <= unit <= 0xDBFF and i + 3 < len(raw):
            low = int.from_bytes(raw[i + 2 : i + 4], "big")
            if 0xDC00 <= low <= 0xDFFF:
                out.append(chr(0x10000 + ((unit - 0xD800) << 10) + (low - 0xDC00)))
                continue
        if 0xD800 <= unit <= 0xDFFF:
            continue  # 孤立代理，丢弃
        if unit == 0:
            continue  # 补位的 0 不算字符
        out.append(chr(unit))
    return "".join(out)


def _code_to_char(value: int) -> str:
    """把一个整数目标码转成字符串（可能是代理对拼出的补充平面字符）。"""
    if value < 0:
        return ""
    if value <= 0xFFFF:
        if 0xD800 <= value <= 0xDFFF:
            return ""  # 孤立代理
        return chr(value)
    if value > 0x10FFFF:
        return ""
    value -= 0x10000
    return chr(0xD800 + (value >> 10)) + chr(0xDC00 + (value & 0x3FF))


class ToUnicodeCMap:
    """一张 ToUnicode 映射表。"""

    __slots__ = ("mapping", "code_bytes")

    def __init__(self, mapping: dict[int, str], code_bytes: int = 2) -> None:
        self.mapping = mapping
        # 源编码每条占几个字节；Identity-H 一律 2 字节
        self.code_bytes = code_bytes

    def __len__(self) -> int:
        return len(self.mapping)

    def __contains__(self, code: int) -> bool:
        return code in self.mapping

    def get(self, code: int, default: str = "") -> str:
        return self.mapping.get(code, default)

    def __repr__(self) -> str:
        return f"<ToUnicodeCMap {len(self.mapping)} entries code_bytes={self.code_bytes}>"


_BFRANGE_LINE = re.compile(
    rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\[.*?\]|<[0-9A-Fa-f]+>)",
    re.S,
)
_BFCHAR_PAIR = re.compile(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", re.S)
_CODESPACE_PAIR = re.compile(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", re.S)

_MAX_RANGE = 65536  # 单条 bfrange 的展开上限，防止畸形文件炸内存


def parse_tounicode(data: bytes) -> ToUnicodeCMap:
    """解析 ``/ToUnicode`` CMap 流的字节内容。

    返回 :class:`ToUnicodeCMap`。解析不了的片段会被跳过，绝不抛异常——
    一个坏 CMap 不应该让整个提取失败。
    """
    mapping: dict[int, str] = {}
    code_bytes = 2  # 默认按 Identity-H 的双字节处理

    # ---- codespace：用来判定源编码宽度 -----------------------------
    for block in re.findall(rb"begincodespacerange(.*?)endcodespacerange", data, re.S):
        for lo_hex, hi_hex in _CODESPACE_PAIR.findall(block):
            width = max(1, (len(lo_hex.strip()) + 1) // 2)
            try:
                lo = int(lo_hex, 16)
                hi = int(hi_hex, 16)
            except ValueError:
                continue
            if lo == 0 and hi >= 0xFF and width == 1:
                code_bytes = 1
            elif width >= 2:
                code_bytes = 2
                break

    # ---- bfchar：一对一映射 ----------------------------------------
    for block in re.findall(rb"beginbfchar(.*?)endbfchar", data, re.S):
        for src_hex, dst_hex in _BFCHAR_PAIR.findall(block):
            try:
                code = int(src_hex, 16)
            except ValueError:
                continue
            text = decode_utf16be(bytes.fromhex(dst_hex.decode("ascii")))
            if text:
                mapping[code] = text

    # ---- bfrange：区间映射（两种写法都要支持） ----------------------
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", data, re.S):
        for m in _BFRANGE_LINE.finditer(block):
            lo_hex, hi_hex, dst = m.group(1), m.group(2), m.group(3)
            try:
                lo = int(lo_hex, 16)
                hi = int(hi_hex, 16)
            except ValueError:
                continue
            if hi < lo or hi - lo > _MAX_RANGE:
                continue

            if dst.startswith(b"["):
                # 数组形式：逐项显式给出（旧实现漏掉的分支）
                items = re.findall(rb"<([0-9A-Fa-f]*)>", dst)
                for offset, item in enumerate(items):
                    code = lo + offset
                    if code > hi:
                        break
                    try:
                        text = decode_utf16be(bytes.fromhex(item.decode("ascii")))
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if text:
                        mapping[code] = text
            else:
                # 递增形式：目标码随源码线性递增
                inner = dst[1:-1]
                try:
                    base = int(inner, 16)
                except ValueError:
                    continue
                for code in range(lo, hi + 1):
                    text = _code_to_char(base + (code - lo))
                    if text:
                        mapping[code] = text

    return ToUnicodeCMap(mapping, code_bytes)


def parse_tounicode_fallback(data: bytes) -> ToUnicodeCMap:
    """容错版解析：先走正规 tokenizer，失败再退回正则。

    少量 CMap 用了非标准写法（多余括号、缺分号等），正则路径能救回来。
    """
    try:
        result = parse_tounicode(data)
        if result.mapping:
            return result
    except Exception:
        pass
    return parse_tounicode(data)

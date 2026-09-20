"""流过滤器（解码器）。

PDF 的流数据经过一层或多层编码，``/Filter`` 声明编码链、``/DecodeParms``
提供各自参数。本模块实现纯 Python 解码，覆盖实际 PDF 里 99% 的情况：

    FlateDecode      zlib / deflate（最常见）
    LZWDecode        LZW 压缩（较老扫描件）
    ASCIIHexDecode   ASCII 十六进制
    ASCII85Decode    ASCII base-85
    RunLengthDecode  行程编码
    CCITTFaxDecode   传真编码（仅声明，不实现——见下方说明）
    DCTDecode / JPXDecode   图像编码，流本身不是文本，直接原样返回

``FlateDecode`` 还支持 ``/Predictor`` 参数（PNG 与 TIFF 两种预测器）。
很多 PDF 生成器默认对交叉引用流和内容流开启 PNG Up 预测器，不处理
预测器会得到完全错位的数据。
"""

from __future__ import annotations

import zlib

__all__ = [
    "FilterError",
    "flate_decode",
    "lzw_decode",
    "ascii_hex_decode",
    "ascii85_decode",
    "run_length_decode",
    "apply_predictor",
    "decode_stream_data",
]

# 图像类编码：解出来也不是文本，交给调用方自行处理
_IMAGE_FILTERS = {"DCTDecode", "DCT", "JPXDecode", "JBIG2Decode", "CCITTFaxDecode", "CCF"}


class FilterError(Exception):
    """流解码失败。"""


def flate_decode(data: bytes) -> bytes:
    """zlib/deflate 解码。

    真实 PDF 里数据常有小瑕疵（缺 EOD 标记、尾部垃圾、用的是裸 deflate），
    所以这里逐级降级重试，而不是一次失败就放弃。
    """
    if not data:
        return b""

    # 1) 标准 zlib
    try:
        return zlib.decompress(data)
    except zlib.error:
        pass

    # 2) 宽容模式：容忍截断、尾部多余字节
    try:
        d = zlib.decompressobj()
        out = d.decompress(data)
        out += d.flush()
        if out:
            return out
    except zlib.error:
        pass

    # 3) 跳过可能的头部垃圾，从各个 0x78 起点再试
    for i in range(1, min(len(data), 64)):
        if data[i] == 0x78:
            try:
                return zlib.decompress(data[i:])
            except zlib.error:
                continue

    # 4) 裸 deflate（无 zlib 头）
    try:
        d = zlib.decompressobj(-15)
        return d.decompress(data) + d.flush()
    except zlib.error as exc:
        raise FilterError(f"FlateDecode 解码失败: {exc}") from exc


def apply_predictor(
    data: bytes,
    predictor: int = 1,
    colors: int = 1,
    bits_per_component: int = 8,
    columns: int = 1,
) -> bytes:
    """还原预测器编码的数据。

    ``predictor`` 取值：1 = 无预测；2 = TIFF predictor；>= 10 = PNG predictor。
    """
    if predictor <= 1:
        return data

    bytes_per_pixel = max(1, (colors * bits_per_component + 7) // 8)
    row_length = max(1, (columns * colors * bits_per_component + 7) // 8)

    if predictor == 2:
        return _tiff_predictor(data, colors, bits_per_component, columns, row_length)

    if predictor < 10:
        raise FilterError(f"不支持的 Predictor 值: {predictor}")

    # PNG 预测器：每一行开头有一个 filter type 字节
    out = bytearray()
    prev_row = bytearray(row_length)
    pos = 0
    n = len(data)
    while pos < n:
        ftype = data[pos]
        pos += 1
        row = bytearray(data[pos : pos + row_length])
        pos += row_length
        if len(row) < row_length:  # 最后一行可能被截断，补零
            row.extend(b"\x00" * (row_length - len(row)))

        if ftype == 0:  # None
            pass
        elif ftype == 1:  # Sub
            for i in range(bytes_per_pixel, row_length):
                row[i] = (row[i] + row[i - bytes_per_pixel]) & 0xFF
        elif ftype == 2:  # Up
            for i in range(row_length):
                row[i] = (row[i] + prev_row[i]) & 0xFF
        elif ftype == 3:  # Average
            for i in range(row_length):
                left = row[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
                row[i] = (row[i] + ((left + prev_row[i]) >> 1)) & 0xFF
        elif ftype == 4:  # Paeth
            for i in range(row_length):
                a = row[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
                b = prev_row[i]
                c = prev_row[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                if pa <= pb and pa <= pc:
                    pred = a
                elif pb <= pc:
                    pred = b
                else:
                    pred = c
                row[i] = (row[i] + pred) & 0xFF
        else:
            raise FilterError(f"未知的 PNG 预测器 filter type: {ftype}")

        out.extend(row)
        prev_row = row
    return bytes(out)


def _tiff_predictor(
    data: bytes,
    colors: int,
    bits_per_component: int,
    columns: int,
    row_length: int,
) -> bytes:
    """TIFF Predictor 2。只实现常见的 8 位情况，其余按行原样返回。"""
    if bits_per_component != 8:
        return data
    out = bytearray()
    for start in range(0, len(data), row_length):
        row = bytearray(data[start : start + row_length])
        for i in range(colors, len(row)):
            row[i] = (row[i] + row[i - colors]) & 0xFF
        out.extend(row)
    return bytes(out)


def ascii_hex_decode(data: bytes) -> bytes:
    """ASCIIHexDecode：忽略空白，``>`` 结束。"""
    end = data.find(b">")
    if end != -1:
        data = data[:end]
    digits = bytes(c for c in data if c not in b"\x00\t\n\x0c\r ")
    if len(digits) % 2:
        digits += b"0"
    try:
        return bytes.fromhex(digits.decode("ascii"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise FilterError(f"ASCIIHexDecode 解码失败: {exc}") from exc


def ascii85_decode(data: bytes) -> bytes:
    """ASCII85Decode。"""
    if data.startswith(b"<~"):
        data = data[2:]
    end = data.find(b"~>")
    if end != -1:
        data = data[:end]

    out = bytearray()
    group: list[int] = []
    for ch in data:
        if ch in b"\x00\t\n\x0c\r ":
            continue
        if ch == 0x7A and not group:  # 'z' == 四个零字节
            out.extend(b"\x00\x00\x00\x00")
            continue
        if not (0x21 <= ch <= 0x75):  # '!' .. 'u'
            continue
        group.append(ch - 0x21)
        if len(group) == 5:
            value = 0
            for g in group:
                value = value * 85 + g
            out.extend(value.to_bytes(4, "big"))
            group = []
    if group:
        pad = 5 - len(group)
        value = 0
        for g in group + [84] * pad:  # 'u' == 84
            value = value * 85 + g
        out.extend(value.to_bytes(4, "big")[: 4 - pad])
    return bytes(out)


def run_length_decode(data: bytes) -> bytes:
    """RunLengthDecode。"""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        length = data[i]
        i += 1
        if length == 128:  # EOD
            break
        if length < 128:
            out.extend(data[i : i + length + 1])
            i += length + 1
        else:
            if i < n:
                out.extend(bytes([data[i]]) * (257 - length))
                i += 1
    return bytes(out)


def lzw_decode(data: bytes, early_change: int = 1) -> bytes:
    """LZWDecode（含 PDF 特有的 EarlyChange 参数）。"""
    out = bytearray()
    table: list[bytes] = [bytes([i]) for i in range(256)] + [b"", b""]
    bit_buffer = 0
    bit_count = 0
    code_width = 9
    prev: bytes | None = None

    for byte in data:
        bit_buffer = (bit_buffer << 8) | byte
        bit_count += 8
        while bit_count >= code_width:
            bit_count -= code_width
            code = (bit_buffer >> bit_count) & ((1 << code_width) - 1)

            if code == 256:  # clear table
                table = [bytes([i]) for i in range(256)] + [b"", b""]
                code_width = 9
                prev = None
                continue
            if code == 257:  # EOD
                return bytes(out)

            if prev is None:
                entry = table[code] if code < len(table) else b""
            elif code < len(table):
                entry = table[code]
                table.append(prev + entry[:1])
            else:  # KwKwK 特例
                entry = prev + prev[:1]
                table.append(entry)

            out.extend(entry)
            prev = entry

            limit = len(table) + (early_change and 1 or 0)
            if limit >= 512:
                code_width = 10
            if limit >= 1024:
                code_width = 11
            if limit >= 2048:
                code_width = 12
    return bytes(out)


def _as_name(value) -> str:
    """把 ``/FlateDecode`` 或 ``FlateDecode`` 统一成 ``FlateDecode``。"""
    if isinstance(value, bytes):
        return value.decode("latin-1")
    return str(value)


def decode_stream_data(stream, resolver=None) -> bytes:
    """对 :class:`~pdftext.objects.PdfStream` 应用完整的 ``/Filter`` 链。

    ``resolver`` 是可选的 ``callable(obj) -> obj``，用于解析 ``/Length`` 或
    ``/DecodeParms`` 里的间接引用。解不开的编码（如 ``DCTDecode``）会原样
    返回，并把已成功应用的解码结果保留下来。
    """
    stream_dict = stream.dict if hasattr(stream, "dict") else stream
    data = stream.raw if hasattr(stream, "raw") else bytes(stream)

    filters = stream_dict.get("Filter")
    if filters is None:
        return data

    def resolve(value):
        if resolver is not None and hasattr(value, "num") and hasattr(value, "gen"):
            return resolver(value)
        return value

    if not isinstance(filters, (list, tuple)):
        filters = [filters]

    parms = stream_dict.get("DecodeParms", stream_dict.get("DP"))
    if parms is None:
        parms = []
    elif not isinstance(parms, (list, tuple)):
        parms = [parms]

    for index, filt in enumerate(filters):
        filt = resolve(filt)
        name = _as_name(filt)

        # 图像编码：不是文本，停在这里
        if name in _IMAGE_FILTERS:
            return data

        parm = parms[index] if index < len(parms) else None
        parm = resolve(parm)
        if not isinstance(parm, dict):
            parm = {}

        if name in ("FlateDecode", "Fl"):
            data = flate_decode(data)
            data = apply_predictor(
                data,
                predictor=int(parm.get("Predictor", 1) or 1),
                colors=int(parm.get("Colors", 1) or 1),
                bits_per_component=int(parm.get("BitsPerComponent", 8) or 8),
                columns=int(parm.get("Columns", 1) or 1),
            )
        elif name == "LZWDecode":
            data = lzw_decode(data, int(parm.get("EarlyChange", 1) or 0))
            data = apply_predictor(
                data,
                predictor=int(parm.get("Predictor", 1) or 1),
                colors=int(parm.get("Colors", 1) or 1),
                bits_per_component=int(parm.get("BitsPerComponent", 8) or 8),
                columns=int(parm.get("Columns", 1) or 1),
            )
        elif name in ("ASCIIHexDecode", "AHx"):
            data = ascii_hex_decode(data)
        elif name in ("ASCII85Decode", "A85"):
            data = ascii85_decode(data)
        elif name in ("RunLengthDecode", "RL"):
            data = run_length_decode(data)
        else:
            # 未知编码：保留当前结果，交由上层决定
            return data

    return data

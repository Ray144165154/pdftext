"""PDF 对象模型与词法/语法解析。

本模块是整个项目的地基，实现 PDF 1.7 规范第 7.3 节定义的八种基本对象类型：

    boolean   true / false
    numeric   整数与实数
    string    字面量字符串 ( ... ) 与十六进制字符串 < ... >
    name      /Name，支持 #XX 转义
    array     [ ... ]
    dictionary << /Key value ... >>
    stream    dictionary + 原始字节
    null      null

这里刻意不依赖任何第三方库，只使用 Python 标准库。

关键设计：
  * :class:`Lexer` 是唯一的词法分析器，对象解析器和内容流解释器共用它，
    避免"两套 tokenizer 行为不一致"这类隐蔽 bug。
  * :class:`Parser` 在 :class:`Lexer` 之上做递归下降，支持回溯（用于 ``R``
    间接引用的三 token 前瞻）。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

__all__ = [
    "PdfName",
    "PdfRef",
    "PdfDict",
    "PdfArray",
    "PdfStream",
    "PdfNull",
    "NULL",
    "Lexer",
    "Parser",
    "ParseError",
    "tokenize",
]


class ParseError(Exception):
    """PDF 语法错误。"""


class PdfNull:
    """``null`` 的单一实例类型。"""

    _instance = None

    def __new__(cls) -> PdfNull:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "null"

    def __bool__(self) -> bool:
        return False


NULL = PdfNull()


class PdfName(str):
    """PDF 名称对象。``/Foo`` 解析为 ``PdfName("Foo")``。

    用 ``str`` 子类而不是普通字符串，是为了在字典里把"名称"和"字符串值"
    区分开——``/Type`` 与 ``(Type)`` 是不同的对象，混用会静默出错。
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return f"/{str(self)}"


@dataclass(frozen=True)
class PdfRef:
    """间接引用，例如 ``12 0 R``。"""

    num: int
    gen: int = 0

    def __repr__(self) -> str:
        return f"{self.num} {self.gen} R"


class PdfDict(dict):
    """PDF 字典。键统一为 ``str``（不含前导斜杠），值保持原样。"""

    def get_name(self, key: str, default: Any = None) -> Any:
        v = self.get(key, default)
        return v

    def get_int(self, key: str, default: int | None = None) -> int | None:
        v = self.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return int(v)
        return default

    def get_number(self, key: str, default: Any = None) -> Any:
        v = self.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v
        return default


class PdfArray(list):
    """PDF 数组。"""


class PdfStream:
    """PDF 流对象：一个字典加上未解码的原始字节。

    ``raw`` 永远是文件里的原始字节，尚未应用 ``/Filter``。解码是
    :mod:`pdftext.filters` 的职责，这样流对象本身不需要知道
    :class:`~pdftext.document.PdfDocument` 的存在。
    """

    __slots__ = ("dict", "raw", "objnum", "gen")

    def __init__(
        self,
        d: PdfDict | dict,
        raw: bytes,
        objnum: int | None = None,
        gen: int = 0,
    ) -> None:
        self.dict = d if isinstance(d, PdfDict) else PdfDict(d)
        self.raw = raw
        self.objnum = objnum
        self.gen = gen

    def __repr__(self) -> str:
        return f"<PdfStream {self.objnum} len={len(self.raw)} {dict.__repr__(self.dict)}>"


# --------------------------------------------------------------------------
# 词法分析
# --------------------------------------------------------------------------

_WHITESPACE = b"\x00\t\n\x0c\r "
_DELIMITERS = b"()<>[]{}/%"
_REGULAR_END = _WHITESPACE + _DELIMITERS

# 数字：允许 .5 / 5. / -.002 等所有 PDF 规范的写法
_NUMBER_CHARS = b"0123456789+-."


def _is_regular(byte: int) -> bool:
    return byte not in _REGULAR_END


class Lexer:
    """PDF 词法分析器。

    产出的 token 是 ``(kind, value)`` 二元组，kind 取值：

    ``num``         -> float
    ``name``        -> str（不含斜杠，#XX 已解码）
    ``str``         -> bytes（字面量字符串，转义已处理）
    ``hexstr``      -> bytes
    ``arr_open`` / ``arr_close``
    ``dict_open`` / ``dict_close``
    ``kw``          -> bytes（关键字/操作符，如 ``obj`` ``Tj`` ``R``）
    ``eof``         -> None
    """

    __slots__ = ("buf", "pos", "end", "_peeked", "_has_peek")

    def __init__(self, buf: bytes, pos: int = 0, end: int | None = None) -> None:
        self.buf = buf
        self.pos = pos
        self.end = len(buf) if end is None else end
        self._peeked: tuple[str, Any] | None = None
        self._has_peek = False

    # -- 位置管理（供回溯使用） ------------------------------------------
    def mark(self) -> tuple[int, tuple[str, Any] | None, bool]:
        return (self.pos, self._peeked, self._has_peek)

    def reset(self, mark: tuple[int, tuple[str, Any] | None, bool]) -> None:
        self.pos, self._peeked, self._has_peek = mark

    # -- 基础扫描 --------------------------------------------------------
    def _skip_ws(self) -> None:
        buf, end = self.buf, self.end
        pos = self.pos
        while pos < end:
            c = buf[pos]
            if c in _WHITESPACE:
                pos += 1
            elif c == 0x25:  # '%' 注释直到行尾
                while pos < end and buf[pos] not in b"\r\n":
                    pos += 1
            else:
                break
        self.pos = pos

    def peek(self) -> tuple[str, Any]:
        if not self._has_peek:
            self._peeked = self._scan()
            self._has_peek = True
        assert self._peeked is not None
        return self._peeked

    def next(self) -> tuple[str, Any]:
        if self._has_peek:
            self._has_peek = False
            assert self._peeked is not None
            tok = self._peeked
            self._peeked = None
            return tok
        return self._scan()

    # -- 实际扫描 --------------------------------------------------------
    def _scan(self) -> tuple[str, Any]:
        self._skip_ws()
        buf, end = self.buf, self.end
        pos = self.pos
        if pos >= end:
            return ("eof", None)
        c = buf[pos]

        # 字典 << 与十六进制字符串 <
        if c == 0x3C:  # '<'
            if pos + 1 < end and buf[pos + 1] == 0x3C:
                self.pos = pos + 2
                return ("dict_open", None)
            return self._scan_hex_string()

        if c == 0x3E:  # '>'
            if pos + 1 < end and buf[pos + 1] == 0x3E:
                self.pos = pos + 2
                return ("dict_close", None)
            raise ParseError(f"位置 {pos}: 出现了孤立的 '>'")

        if c == 0x5B:  # '['
            self.pos = pos + 1
            return ("arr_open", None)
        if c == 0x5D:  # ']'
            self.pos = pos + 1
            return ("arr_close", None)

        if c == 0x2F:  # '/'
            return self._scan_name()
        if c == 0x28:  # '('
            return self._scan_literal_string()

        # 数字
        if c in b"+-.0123456789":
            return self._scan_number_or_keyword()

        # 关键字 / 操作符
        return self._scan_keyword()

    def _scan_name(self) -> tuple[str, Any]:
        buf, end = self.buf, self.end
        pos = self.pos + 1  # 跳过 '/'
        out = bytearray()
        while pos < end and _is_regular(buf[pos]):
            ch = buf[pos]
            if ch == 0x23 and pos + 2 < end:  # '#'
                try:
                    out.append(int(buf[pos + 1 : pos + 3], 16))
                    pos += 3
                    continue
                except ValueError:
                    pass  # 非法转义，按字面处理
            out.append(ch)
            pos += 1
        self.pos = pos
        return ("name", out.decode("latin-1"))

    def _scan_number_or_keyword(self) -> tuple[str, Any]:
        buf, end = self.buf, self.end
        pos = self.pos
        start = pos
        while pos < end and buf[pos] in _NUMBER_CHARS:
            pos += 1
        # 形如 "5." / "5.0.3" 的畸形输入，回退到按关键字处理
        text = buf[start:pos]
        self.pos = pos
        try:
            s = text.decode("ascii")
            if s.count(".") > 1 or s in ("+", "-", ".", "+.", "-."):
                raise ValueError
            value = float(s)
        except (UnicodeDecodeError, ValueError):
            self.pos = start
            return self._scan_keyword()
        # 整数值保持 int，便于 objnum 之类做精确比较
        if value.is_integer() and b"." not in text and b"e" not in text and b"E" not in text:
            return ("num", int(value))
        return ("num", value)

    def _scan_keyword(self) -> tuple[str, Any]:
        buf, end = self.buf, self.end
        pos = self.pos
        start = pos
        while pos < end and _is_regular(buf[pos]):
            pos += 1
        if pos == start:  # 不认识的定界符，跳过以免死循环
            self.pos = pos + 1
            return ("kw", buf[start : start + 1])
        self.pos = pos
        return ("kw", buf[start:pos])

    def _scan_hex_string(self) -> tuple[str, Any]:
        buf, end = self.buf, self.end
        pos = self.pos + 1
        out = bytearray()
        while pos < end and buf[pos] != 0x3E:  # '>'
            ch = buf[pos]
            if ch not in _WHITESPACE:
                out.append(ch)
            pos += 1
        self.pos = min(pos + 1, end)
        if len(out) % 2:  # 规范要求补一个 0
            out.append(0x30)
        try:
            return ("hexstr", bytes.fromhex(out.decode("ascii")))
        except (ValueError, UnicodeDecodeError):
            return ("hexstr", b"")

    def _scan_literal_string(self) -> tuple[str, Any]:
        buf, end = self.buf, self.end
        pos = self.pos + 1
        depth = 1
        out = bytearray()
        while pos < end:
            ch = buf[pos]
            if ch == 0x5C:  # 反斜杠转义
                pos += 1
                if pos >= end:
                    break
                nxt = buf[pos]
                simple = {
                    0x6E: 0x0A,  # \n
                    0x72: 0x0D,  # \r
                    0x74: 0x09,  # \t
                    0x62: 0x08,  # \b
                    0x66: 0x0C,  # \f
                    0x28: 0x28,  # \(
                    0x29: 0x29,  # \)
                    0x5C: 0x5C,  # \\
                }
                if nxt in simple:
                    out.append(simple[nxt])
                    pos += 1
                elif 0x30 <= nxt <= 0x37:  # 八进制，最多 3 位
                    j = pos
                    oct_digits = bytearray()
                    while j < end and len(oct_digits) < 3 and 0x30 <= buf[j] <= 0x37:
                        oct_digits.append(buf[j])
                        j += 1
                    out.append(int(oct_digits, 8) & 0xFF)
                    pos = j
                elif nxt in (0x0A, 0x0D):  # 行继续：反斜杠 + 换行 = 空
                    pos += 1
                    if nxt == 0x0D and pos < end and buf[pos] == 0x0A:
                        pos += 1
                else:
                    out.append(nxt)
                    pos += 1
                continue
            if ch == 0x28:  # '('
                depth += 1
            elif ch == 0x29:  # ')'
                depth -= 1
                if depth == 0:
                    pos += 1
                    break
            out.append(ch)
            pos += 1
        self.pos = pos
        return ("str", bytes(out))


def tokenize(buf: bytes) -> Iterator[tuple[str, Any]]:
    """把一段字节流拆成 token（生成器）。"""
    lx = Lexer(buf)
    while True:
        tok = lx.next()
        if tok[0] == "eof":
            return
        yield tok


# --------------------------------------------------------------------------
# 语法分析
# --------------------------------------------------------------------------


class Parser:
    """在 :class:`Lexer` 之上解析 PDF 对象。"""

    __slots__ = ("lex",)

    def __init__(self, buf: bytes, pos: int = 0, end: int | None = None) -> None:
        self.lex = Lexer(buf, pos, end)

    @property
    def pos(self) -> int:
        return self.lex.pos

    def at_end(self) -> bool:
        return self.lex.peek()[0] == "eof"

    def parse_object(self, depth: int = 0) -> Any:
        """解析一个完整对象。到达流末尾返回 :data:`NULL`。"""
        if depth > 64:
            raise ParseError("对象嵌套过深（超过 64 层）")
        kind, value = self.lex.next()

        if kind == "num":
            return self._maybe_ref(value)
        if kind == "name":
            return PdfName(value)
        if kind in ("str", "hexstr"):
            return value
        if kind == "arr_open":
            return self._parse_array(depth)
        if kind == "dict_open":
            return self._parse_dict(depth)
        if kind == "kw":
            if value == b"true":
                return True
            if value == b"false":
                return False
            if value == b"null":
                return NULL
            # 内容流里出现裸操作符时不该走到这里；保守返回关键字
            return PdfName(value.decode("latin-1"))
        if kind in ("arr_close", "dict_close"):
            raise ParseError(f"位置 {self.lex.pos}: 意外的 {kind}")
        if kind == "eof":
            return NULL
        raise ParseError(f"位置 {self.lex.pos}: 无法解析的 token {kind!r}")

    def _maybe_ref(self, num: Any) -> Any:
        """判断 ``12 0 R`` 形式的间接引用，否则就是普通数字。"""
        mark = self.lex.mark()
        k1, v1 = self.lex.next()
        if k1 == "num" and isinstance(v1, int) and isinstance(num, int):
            k2, v2 = self.lex.next()
            if k2 == "kw" and v2 == b"R":
                return PdfRef(num, v1)
        self.lex.reset(mark)
        return num

    def _parse_array(self, depth: int) -> PdfArray:
        arr = PdfArray()
        while True:
            kind, _ = self.lex.peek()
            if kind == "arr_close":
                self.lex.next()
                return arr
            if kind == "eof":
                raise ParseError("数组未闭合就遇到了文件末尾")
            arr.append(self.parse_object(depth + 1))

    def _parse_dict(self, depth: int) -> PdfDict:
        d = PdfDict()
        while True:
            kind, value = self.lex.next()
            if kind == "dict_close":
                return d
            if kind == "eof":
                raise ParseError("字典未闭合就遇到了文件末尾")
            if kind != "name":
                raise ParseError(f"字典的键必须是名称，却遇到 {kind!r}")
            d[str(value)] = self.parse_object(depth + 1)

    # -- 流 --------------------------------------------------------------
    def parse_indirect_object(self) -> tuple[int, int, Any]:
        """解析 ``num gen obj ... endobj`` 形式的间接对象。

        返回 ``(objnum, gen, value)``；若遇到 ``stream``，返回
        :class:`PdfStream`（``raw`` 为原始未解码字节）。
        """
        k, num = self.lex.next()
        if k != "num" or not isinstance(num, int):
            raise ParseError(f"位置 {self.lex.pos}: 期望对象号，却遇到 {k!r}")
        k, gen = self.lex.next()
        if k != "num" or not isinstance(gen, int):
            raise ParseError(f"位置 {self.lex.pos}: 期望世代号，却遇到 {k!r}")
        k, kw = self.lex.next()
        if k != "kw" or kw != b"obj":
            raise ParseError(f"位置 {self.lex.pos}: 期望 'obj'，却遇到 {k!r}")

        value = self.parse_object()

        # 字典之后若紧跟 stream 关键字，则读原始流数据
        if isinstance(value, PdfDict):
            mark = self.lex.mark()
            k, kw = self.lex.next()
            if k == "kw" and kw == b"stream":
                # 规范：stream 关键字后必须跟 CRLF 或 LF
                p = self.lex.pos
                buf = self.lex.buf
                if p < len(buf) and buf[p] == 0x0D:
                    p += 1
                if p < len(buf) and buf[p] == 0x0A:
                    p += 1
                length = value.get("/Length") if False else value.get("Length")
                raw = None
                if isinstance(length, int):
                    raw = buf[p : p + length]
                    # 校验：后面应当紧跟 endstream（允许空白）
                    probe = p + length
                    tail = buf[probe : probe + 20]
                    if b"endstream" not in tail:
                        raw = None  # /Length 不可信，退回搜索
                if raw is None:
                    idx = buf.find(b"endstream", p)
                    if idx == -1:
                        raise ParseError("流缺少 endstream")
                    raw = buf[p:idx]
                    if raw.endswith(b"\r\n"):
                        raw = raw[:-2]
                    elif raw.endswith(b"\n") or raw.endswith(b"\r"):
                        raw = raw[:-1]
                    self.lex.pos = idx + len(b"endstream")
                else:
                    self.lex.pos = p + length
                return (num, gen, PdfStream(value, raw, num, gen))
            self.lex.reset(mark)

        return (num, gen, value)

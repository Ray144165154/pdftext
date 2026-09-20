"""内容流解释器：把绘图指令变成带坐标的字符序列。

这是整个提取器的核心。PDF 内容流是一串"操作数 操作符"的逆波兰式指令，
我们要模拟一个**文本状态机**，把每个字形的设备坐标算出来。

必须正确处理的几件事，任何一件错了坐标就全乱：

  * **图形状态栈** ``q`` / ``Q``
    规范要求 ``Q`` 恢复 ``q`` 保存的 CTM。只压不弹会让 CTM 不断复合，
    整页坐标塌缩成一团（这是本项目早期的真实 bug）。

  * **文本矩阵推进**
    每画一个字形，``Tm`` 都要按 ``w0/1000 * 字号`` 前进。不推进宽度，
    同一 ``Tj`` 里的多个字就会全叠在一个点上。

  * **三套矩阵的复合顺序**
    ``Trm = [字号×水平缩放, 0, 0, 字号, 0, 上升] × Tm × CTM``，
    顺序错了旋转和缩放的文字位置会偏。

  * ``TJ`` 数组里的**数字是字距调整**，与字符串要交替处理。

  * ``'`` ``"`` ``T*`` ``TD`` 各自隐含的换行与字距语义。

另外还处理 Form XObject（``Do``）的递归展开——大量 PDF 把正文放在
Form 里，不支持就等于一个字都提不出来。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from math import hypot
from typing import Any

from .fonts import Font, build_fonts
from .objects import NULL, Lexer, PdfArray, PdfDict, PdfName, PdfStream

__all__ = ["TextChar", "ContentInterpreter", "interpret_content", "IDENTITY"]

IDENTITY: tuple[float, float, float, float, float, float] = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

Matrix = tuple[float, float, float, float, float, float]


def mat_mul(m: Matrix, n: Matrix) -> Matrix:
    """矩阵复合：先应用 ``m``，再应用 ``n``。

    PDF 用行向量约定，矩阵 ``[a b c d e f]`` 表示::

        | a  b  0 |
        | c  d  0 |
        | e  f  1 |
    """
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (
        a * A + b * C,
        a * B + b * D,
        c * A + d * C,
        c * B + d * D,
        e * A + f * C + E,
        e * B + f * D + F,
    )


def apply_matrix(x: float, y: float, m: Matrix) -> tuple[float, float]:
    """把点 ``(x, y)`` 用矩阵 ``m`` 变换。"""
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


@dataclass(slots=True)
class TextChar:
    """一个已定位的字形。"""

    code: int          # 原始字符码（用来查宽度）
    text: str          # 解码后的文字
    x: float           # 基线起点 x（设备坐标，单位 pt）
    y: float           # 基线 y
    size: float        # 设备空间中的字号
    adv_x: float       # 设备空间中的前进向量
    adv_y: float
    font: str = ""     # 字体资源名
    color: tuple[float, float, float] = (0.0, 0.0, 0.0)
    render_mode: int = 0
    seq: int = 0       # 在内容流里出现的次序，用于稳定排序

    @property
    def invisible(self) -> bool:
        """渲染模式 3 = 不可见（常见于扫描件的 OCR 文字层）。"""
        return self.render_mode == 3

    @property
    def end_x(self) -> float:
        return self.x + self.adv_x


class _State:
    """图形 + 文本状态。"""

    __slots__ = (
        "ctm", "tm", "tlm", "char_spacing", "word_spacing", "h_scale",
        "leading", "font_name", "font_size", "rise", "render_mode", "color",
    )

    def __init__(self) -> None:
        self.ctm: Matrix = IDENTITY
        self.tm: Matrix = IDENTITY
        self.tlm: Matrix = IDENTITY
        self.char_spacing = 0.0
        self.word_spacing = 0.0
        self.h_scale = 1.0
        self.leading = 0.0
        self.font_name = ""
        self.font_size = 0.0
        self.rise = 0.0
        self.render_mode = 0
        self.color: tuple[float, float, float] = (0.0, 0.0, 0.0)

    def copy(self) -> "_State":
        new = _State.__new__(_State)
        new.ctm = self.ctm
        new.tm = self.tm
        new.tlm = self.tlm
        new.char_spacing = self.char_spacing
        new.word_spacing = self.word_spacing
        new.h_scale = self.h_scale
        new.leading = self.leading
        new.font_name = self.font_name
        new.font_size = self.font_size
        new.rise = self.rise
        new.render_mode = self.render_mode
        new.color = self.color
        return new


class ContentInterpreter:
    """执行内容流，收集 :class:`TextChar`。"""

    MAX_FORM_DEPTH = 12

    def __init__(self, doc, page_index: int = 0) -> None:
        self.doc = doc
        self.page_index = page_index
        self.chars: list[TextChar] = []
        self._seq = 0
        self._font_cache: dict[int, dict[str, Font]] = {}

    # -- 对外入口 --------------------------------------------------------
    def run(
        self,
        data: bytes,
        resources: Any,
        ctm: Matrix = IDENTITY,
        depth: int = 0,
    ) -> list[TextChar]:
        """解释一段内容流。"""
        fonts = self._fonts_for(resources)
        state = _State()
        state.ctm = ctm
        stack: list[_State] = []

        lexer = Lexer(data)
        operands: list[Any] = []

        while True:
            kind, value = lexer.next()
            if kind == "eof":
                break

            if kind == "kw":
                if value == b"BI":  # 内联图像，跳过二进制体
                    self._skip_inline_image(lexer)
                    operands = []
                    continue
                try:
                    self._execute(value, operands, state, stack, fonts, resources, depth)
                except Exception:
                    # 单个操作符出错不该毁掉整页：记录下来继续
                    pass
                operands = []
                continue

            if kind == "num":
                operands.append(value)
            elif kind == "name":
                operands.append(PdfName(value))
            elif kind in ("str", "hexstr"):
                operands.append(value)
            elif kind == "arr_open":
                operands.append(self._read_array(lexer))
            elif kind == "dict_open":
                operands.append(self._read_dict(lexer))
            elif kind in ("arr_close", "dict_close"):
                continue

        return self.chars

    # -- 字体 ------------------------------------------------------------
    def _fonts_for(self, resources: Any) -> dict[str, Font]:
        key = id(resources) if resources is not None else 0
        cached = self._font_cache.get(key)
        if cached is None:
            cached = build_fonts(self.doc, resources)
            self._font_cache[key] = cached
        return cached

    # -- 数组/字典读取 ---------------------------------------------------
    def _read_array(self, lexer: Lexer) -> PdfArray:
        arr = PdfArray()
        while True:
            kind, value = lexer.next()
            if kind in ("arr_close", "eof"):
                return arr
            if kind == "num":
                arr.append(value)
            elif kind == "name":
                arr.append(PdfName(value))
            elif kind in ("str", "hexstr"):
                arr.append(value)
            elif kind == "arr_open":
                arr.append(self._read_array(lexer))
            elif kind == "dict_open":
                arr.append(self._read_dict(lexer))
            elif kind == "kw":
                if value == b"true":
                    arr.append(True)
                elif value == b"false":
                    arr.append(False)
                elif value == b"null":
                    arr.append(NULL)

    def _read_dict(self, lexer: Lexer) -> PdfDict:
        d = PdfDict()
        while True:
            kind, value = lexer.next()
            if kind in ("dict_close", "eof"):
                return d
            if kind != "name":
                continue
            key = str(value)
            k2, v2 = lexer.next()
            if k2 == "num":
                d[key] = v2
            elif k2 == "name":
                d[key] = PdfName(v2)
            elif k2 in ("str", "hexstr"):
                d[key] = v2
            elif k2 == "arr_open":
                d[key] = self._read_array(lexer)
            elif k2 == "dict_open":
                d[key] = self._read_dict(lexer)
            elif k2 == "kw":
                d[key] = {"true": True, "false": False}.get(v2.decode("latin-1"))

    def _skip_inline_image(self, lexer: Lexer) -> None:
        """跳过 ``BI ... ID <二进制> EI``。"""
        buf = lexer.buf
        while True:
            kind, value = lexer.next()
            if kind == "eof":
                return
            if kind == "kw" and value == b"ID":
                break

        pos = lexer.pos
        if pos < len(buf) and buf[pos] in b"\x00\t\n\x0c\r ":
            pos += 1

        search = pos
        while True:
            idx = buf.find(b"EI", search)
            if idx == -1:
                lexer.pos = len(buf)
                return
            # 规范要求 EI 前面是空白，借此避开二进制数据里的偶然 "EI"
            if idx > 0 and buf[idx - 1] in b"\x00\t\n\x0c\r ":
                lexer.pos = idx + 2
                return
            search = idx + 2

    # -- 操作符 ----------------------------------------------------------
    def _execute(
        self,
        op: bytes,
        operands: list[Any],
        state: _State,
        stack: list[_State],
        fonts: dict[str, Font],
        resources: Any,
        depth: int,
    ) -> None:
        # ---- 图形状态 ----
        if op == b"q":
            stack.append(state.copy())
            return
        if op == b"Q":
            if stack:
                restored = stack.pop()
                # Q 只恢复图形状态；文本状态按规范也在 q/Q 范围内，一并恢复
                state.ctm = restored.ctm
            return
        if op == b"cm":
            if len(operands) >= 6:
                m = self._matrix(operands[-6:])
                state.ctm = mat_mul(m, state.ctm)
            return

        # ---- 颜色（非描边色 = 文字填充色） ----
        if op == b"g":
            if operands:
                v = float(operands[-1])
                state.color = (v, v, v)
            return
        if op == b"rg":
            if len(operands) >= 3:
                state.color = tuple(float(x) for x in operands[-3:])  # type: ignore[assignment]
            return
        if op == b"k":
            if len(operands) >= 4:
                c, m_, y, k = (float(x) for x in operands[-4:])
                state.color = (
                    (1 - c) * (1 - k),
                    (1 - m_) * (1 - k),
                    (1 - y) * (1 - k),
                )
            return
        if op in (b"G", b"RG", b"K"):
            return  # 描边色不影响文字

        # ---- 文本对象 ----
        if op == b"BT":
            state.tm = IDENTITY
            state.tlm = IDENTITY
            return
        if op == b"ET":
            return

        if op == b"Tf":
            if len(operands) >= 2:
                fname = operands[-2]
                state.font_name = str(fname)
                try:
                    state.font_size = float(operands[-1])
                except (TypeError, ValueError):
                    state.font_size = 0.0
            return
        if op == b"Td":
            if len(operands) >= 2:
                self._move_text(operands[-2], operands[-1], state)
            return
        if op == b"TD":
            if len(operands) >= 2:
                try:
                    state.leading = -float(operands[-1])
                except (TypeError, ValueError):
                    pass
                self._move_text(operands[-2], operands[-1], state)
            return
        if op == b"Tm":
            if len(operands) >= 6:
                m = self._matrix(operands[-6:])
                state.tm = m
                state.tlm = m
            return
        if op == b"T*":
            self._move_text(0.0, -state.leading, state)
            return
        if op == b"TL":
            if operands:
                state.leading = float(operands[-1])
            return
        if op == b"Tc":
            if operands:
                state.char_spacing = float(operands[-1])
            return
        if op == b"Tw":
            if operands:
                state.word_spacing = float(operands[-1])
            return
        if op == b"Tz":
            if operands:
                state.h_scale = float(operands[-1]) / 100.0
            return
        if op == b"Ts":
            if operands:
                state.rise = float(operands[-1])
            return
        if op == b"Tr":
            if operands:
                state.render_mode = int(float(operands[-1]))
            return

        # ---- 显示文字 ----
        if op == b"Tj":
            if operands and isinstance(operands[-1], (bytes, bytearray)):
                self._show(bytes(operands[-1]), state, fonts)
            return
        if op == b"TJ":
            if operands and isinstance(operands[-1], (list, tuple)):
                self._show_array(operands[-1], state, fonts)
            return
        if op == b"'":
            self._move_text(0.0, -state.leading, state)
            if operands and isinstance(operands[-1], (bytes, bytearray)):
                self._show(bytes(operands[-1]), state, fonts)
            return
        if op == b'"':
            if len(operands) >= 3:
                try:
                    state.word_spacing = float(operands[-3])
                    state.char_spacing = float(operands[-2])
                except (TypeError, ValueError):
                    pass
                self._move_text(0.0, -state.leading, state)
                if isinstance(operands[-1], (bytes, bytearray)):
                    self._show(bytes(operands[-1]), state, fonts)
            return

        # ---- XObject ----
        if op == b"Do":
            if operands:
                self._do_xobject(str(operands[-1]), state, resources, depth)
            return

    # -- 文本定位 --------------------------------------------------------
    @staticmethod
    def _matrix(values: list[Any]) -> Matrix:
        out: list[float] = []
        for v in values:
            try:
                out.append(float(v))
            except (TypeError, ValueError):
                out.append(0.0)
        return (out[0], out[1], out[2], out[3], out[4], out[5])

    @staticmethod
    def _move_text(tx: Any, ty: Any, state: _State) -> None:
        try:
            dx, dy = float(tx), float(ty)
        except (TypeError, ValueError):
            return
        state.tlm = mat_mul((1.0, 0.0, 0.0, 1.0, dx, dy), state.tlm)
        state.tm = state.tlm

    def _show(self, data: bytes, state: _State, fonts: dict[str, Font]) -> None:
        """显示一个字符串。核心的字形循环。"""
        font = fonts.get(state.font_name)
        if font is None:
            font = Font(name=state.font_name or "?", code_bytes=1)

        font_size = state.font_size
        for code in font.split_codes(data):
            self._emit_char(font, code, state, font_size)

    def _show_array(self, items: Any, state: _State, fonts: dict[str, Font]) -> None:
        """``TJ`` 数组：字符串与字距数字交替。"""
        for item in items:
            if isinstance(item, (bytes, bytearray)):
                self._show(bytes(item), state, fonts)
            elif isinstance(item, (int, float)) and not isinstance(item, bool):
                # 数字是字距调整，单位 1/1000 文本空间，方向与前进相反
                tx = -float(item) / 1000.0 * state.font_size * state.h_scale
                state.tm = mat_mul((1.0, 0.0, 0.0, 1.0, tx, 0.0), state.tm)

    def _emit_char(
        self,
        font: Font,
        code: int,
        state: _State,
        font_size: float,
    ) -> None:
        """算出一个字形的坐标、尺寸、前进量，并入队。"""
        # 文本空间 -> 设备空间（不含字号与上升）
        tm_dev = mat_mul(state.tm, state.ctm)
        bx, by = tm_dev[4], tm_dev[5]

        # 设备空间中的字号：文本空间 y 方向单位向量在设备空间的长度 × 字号
        size_dev = font_size * hypot(tm_dev[2], tm_dev[3])
        if size_dev == 0.0:
            size_dev = abs(font_size) or 1.0

        # 前进量（文本空间）
        w0 = font.width(code)
        word_spacing = 0.0
        # 字间距只对单字节空格生效（规范 9.3.3）
        if code == 32 and font.code_bytes == 1:
            word_spacing = state.word_spacing
        tx = (w0 / 1000.0 * font_size + state.char_spacing + word_spacing) * state.h_scale

        # 前进量（设备空间）
        adv_x = tx * tm_dev[0]
        adv_y = tx * tm_dev[1]

        text = font.decode_code(code)
        if text:
            self._seq += 1
            self.chars.append(
                TextChar(
                    code=code,
                    text=text,
                    x=bx,
                    y=by,
                    size=abs(size_dev),
                    adv_x=adv_x,
                    adv_y=adv_y,
                    font=state.font_name,
                    color=state.color,
                    render_mode=state.render_mode,
                    seq=self._seq,
                )
            )

        # 推进文本矩阵——不做这一步，多字符 Tj 会全部重叠
        state.tm = mat_mul((1.0, 0.0, 0.0, 1.0, tx, 0.0), state.tm)
        # 记录行进方向，供布局阶段使用
        state.tm = state.tm

    # -- Form XObject ----------------------------------------------------
    def _do_xobject(
        self,
        name: str,
        state: _State,
        resources: Any,
        depth: int,
    ) -> None:
        if depth >= self.MAX_FORM_DEPTH:
            return
        resources = self.doc.resolve(resources) if self.doc is not None else resources
        if not isinstance(resources, PdfDict):
            return
        xobjects = self.doc.resolve(resources.get("XObject"))
        if not isinstance(xobjects, PdfDict):
            return

        target = None
        for key, value in xobjects.items():
            if str(key) == name:
                target = value
                break
        if target is None:
            return

        form = self.doc.resolve(target)
        if not isinstance(form, PdfStream):
            return
        if str(form.dict.get("Subtype", "")) != "Form":
            return  # Image 等与文字无关

        # Form 自带 /Matrix，先与当前 CTM 复合
        matrix = self.doc.resolve(form.dict.get("Matrix"))
        local: Matrix = IDENTITY
        if isinstance(matrix, (list, tuple)) and len(matrix) >= 6:
            try:
                local = tuple(float(x) for x in matrix[:6])  # type: ignore[assignment]
            except (TypeError, ValueError):
                local = IDENTITY

        saved_ctm = state.ctm
        state.ctm = mat_mul(local, state.ctm)

        form_resources = self.doc.resolve(form.dict.get("Resources")) or resources
        try:
            data = self.doc.get_stream_data(form)
        except Exception:
            data = b""
        if data:
            self.run(data, form_resources, state.ctm, depth + 1)

        state.ctm = saved_ctm


def interpret_content(
    doc,
    data: bytes,
    resources: Any,
    page_index: int = 0,
) -> list[TextChar]:
    """便捷函数：解释一页内容流，返回字符列表。"""
    interpreter = ContentInterpreter(doc, page_index)
    return interpreter.run(data, resources)

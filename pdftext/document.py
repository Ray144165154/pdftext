"""PDF 文档层：交叉引用表、对象加载、页面树遍历。

相较常见的"正则扫 ``N G obj``"做法，本模块实现真正的交叉引用解析：

  * 传统 xref 表（``xref`` 关键字 + ``trailer``）
  * 交叉引用流 ``/Type /XRef``（PDF 1.5+，含 ``/W`` ``/Index``）
  * 对象流 ``/Type /ObjStm``（PDF 1.5+ 把多个对象压进一个流）
  * ``/Prev`` 增量更新链、``/XRefStm`` 混合引用文件

当交叉引用表损坏时（真实世界的 PDF 经常如此），自动降级为全文件扫描重建，
而不是直接报错退出。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from . import filters
from .objects import NULL, ParseError, Parser, PdfArray, PdfDict, PdfRef, PdfStream

__all__ = ["PdfDocument", "DocumentError"]


class DocumentError(Exception):
    """文档结构错误。"""


# 交叉引用条目的三种类型
_FREE = 0
_IN_FILE = 1
_IN_OBJSTM = 2

_OBJ_RE = re.compile(rb"(?<![0-9])(\d{1,10})\s+(\d{1,5})\s+obj\b")
_STARTXREF_RE = re.compile(rb"startxref\s+(\d+)")
_TRAILER_RE = re.compile(rb"trailer")


class PdfDocument:
    """一个解析好的 PDF 文档。

    典型用法::

        doc = PdfDocument.from_file("input.pdf")
        print(doc.page_count)
        for page in doc.pages:
            ...
    """

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.version = self._read_version(data)

        # objnum -> (type, a, b)
        self.xref: dict[int, tuple[int, int, int]] = {}
        self.trailer: PdfDict = PdfDict()
        self._cache: dict[int, Any] = {}
        self._objstm_cache: dict[int, dict[int, Any]] = {}
        self._loading: set[int] = set()
        self.repaired = False

        try:
            self._load_xref()
        except Exception:
            self.xref = {}
            self.trailer = PdfDict()

        if not self.xref or "Root" not in self.trailer:
            self._rebuild_xref()

        if "Encrypt" in self.trailer:
            raise DocumentError(
                "该 PDF 已加密（/Encrypt）。本工具不实现解密，请先用 qpdf --decrypt 处理。"
            )

    # -- 构造 ------------------------------------------------------------
    @classmethod
    def from_file(cls, path) -> PdfDocument:
        with open(path, "rb") as fh:
            return cls(fh.read())

    @classmethod
    def from_bytes(cls, data: bytes) -> PdfDocument:
        return cls(data)

    @staticmethod
    def _read_version(data: bytes) -> str:
        # 头部可能不在文件最开头（少量生成器会前置垃圾字节），在前 1024 字节内找
        head = data[:1024]
        idx = head.find(b"%PDF-")
        if idx == -1:
            return "1.4"  # 容错：没有头部也继续尝试
        return head[idx + 5 : idx + 9].decode("latin-1", "replace").strip()

    # -- 交叉引用 --------------------------------------------------------
    def _load_xref(self) -> None:
        matches = list(_STARTXREF_RE.finditer(self.data))
        if not matches:
            raise DocumentError("找不到 startxref，交叉引用表缺失")

        # 从最后一个 startxref 开始，沿 /Prev 往回走
        seen: set[int] = set()
        offset = int(matches[-1].group(1))
        while offset is not None and offset not in seen and 0 <= offset < len(self.data):
            seen.add(offset)
            offset = self._load_xref_section(offset)

    def _load_xref_section(self, offset: int) -> int | None:
        """读一个交叉引用段，返回 ``/Prev`` 指向的偏移（没有则 None）。"""
        buf = self.data
        # 允许少量偏移误差（有些文件的 startxref 差几个字节）
        for probe in (offset, offset + 1, offset - 1, offset + 2):
            if probe < 0 or probe >= len(buf):
                continue
            chunk = buf[probe : probe + 32]
            if chunk.lstrip()[:4] == b"xref":
                return self._load_xref_table(buf.find(b"xref", probe))
            if self._looks_like_xref_stream(probe):
                return self._load_xref_stream(probe)
        raise DocumentError(f"偏移 {offset} 处不是有效的交叉引用段")

    def _looks_like_xref_stream(self, offset: int) -> bool:
        try:
            parser = Parser(self.data, offset)
            _, _, obj = parser.parse_indirect_object()
        except (ParseError, Exception):
            return False
        return isinstance(obj, PdfStream) and str(obj.dict.get("Type", "")) == "XRef"

    def _load_xref_table(self, offset: int) -> int | None:
        """解析传统 xref 表。"""
        buf = self.data
        pos = offset + 4  # 跳过 'xref'

        while True:
            # 跳过空白
            while pos < len(buf) and buf[pos] in b"\x00\t\n\x0c\r ":
                pos += 1
            if buf[pos : pos + 7] == b"trailer":
                break
            # 读 subsection 头：<start> <count>
            line_end = buf.find(b"\n", pos)
            if line_end == -1:
                break
            header = buf[pos:line_end].split()
            if len(header) != 2:
                break
            try:
                start, count = int(header[0]), int(header[1])
            except ValueError:
                break
            pos = line_end + 1

            for i in range(count):
                entry = buf[pos : pos + 20]
                parts = entry.split()
                if len(parts) < 3:
                    break
                try:
                    off = int(parts[0])
                    gen = int(parts[1])
                    typ = parts[2][:1]
                except ValueError:
                    break
                num = start + i
                if typ == b"n":
                    # 先出现的条目优先（后读的旧段不应覆盖新段）
                    self.xref.setdefault(num, (_IN_FILE, off, gen))
                pos += 20

        # 读 trailer 字典
        parser = Parser(buf, pos)
        kind, kw = parser.lex.next()
        if kind == "kw" and kw == b"trailer":
            trailer = parser.parse_object()
            if isinstance(trailer, PdfDict):
                self._merge_trailer(trailer)
                # 混合引用文件
                stm = trailer.get("XRefStm")
                if isinstance(stm, int):
                    try:
                        self._load_xref_stream(stm)
                    except Exception:
                        pass
                prev = trailer.get("Prev")
                if isinstance(prev, int):
                    return prev
        return None

    def _load_xref_stream(self, offset: int) -> int | None:
        """解析交叉引用流（PDF 1.5+）。"""
        parser = Parser(self.data, offset)
        _, _, obj = parser.parse_indirect_object()
        if not isinstance(obj, PdfStream):
            raise DocumentError("交叉引用流解析失败")

        data = filters.decode_stream_data(obj, self._resolve_for_filter)
        widths = obj.dict.get("W")
        if not isinstance(widths, (list, tuple)) or len(widths) < 3:
            raise DocumentError("交叉引用流缺少 /W")
        w0, w1, w2 = (int(x) for x in widths[:3])
        entry_size = w0 + w1 + w2
        if entry_size <= 0:
            raise DocumentError("交叉引用流 /W 非法")

        size = obj.dict.get("Size")
        index = obj.dict.get("Index")
        if not isinstance(index, (list, tuple)):
            index = [0, int(size) if isinstance(size, int) else len(data) // entry_size]

        fields = []
        for i in range(0, len(index) - 1, 2):
            try:
                fields.append((int(index[i]), int(index[i + 1])))
            except (TypeError, ValueError):
                continue

        pos = 0
        for start, count in fields:
            for i in range(count):
                if pos + entry_size > len(data):
                    break
                chunk = data[pos : pos + entry_size]
                pos += entry_size
                f0 = int.from_bytes(chunk[:w0], "big") if w0 else 1
                f1 = int.from_bytes(chunk[w0 : w0 + w1], "big") if w1 else 0
                f2 = int.from_bytes(chunk[w0 + w1 :], "big") if w2 else 0
                num = start + i
                if f0 == _IN_FILE:
                    self.xref.setdefault(num, (_IN_FILE, f1, f2))
                elif f0 == _IN_OBJSTM:
                    self.xref.setdefault(num, (_IN_OBJSTM, f1, f2))

        self._merge_trailer(obj.dict)
        prev = obj.dict.get("Prev")
        return int(prev) if isinstance(prev, int) else None

    def _merge_trailer(self, trailer: PdfDict) -> None:
        for key, value in trailer.items():
            if key == "Prev":
                continue
            # 新段（先读到的）优先，旧段不覆盖
            self.trailer.setdefault(key, value)

    def _resolve_for_filter(self, ref):
        if isinstance(ref, PdfRef):
            return self.get_object(ref.num, ref.gen)
        return ref

    # -- 交叉引用重建（容错路径） ----------------------------------------
    def _rebuild_xref(self) -> None:
        """全文件扫描 ``N G obj`` 重建交叉引用表。

        当 xref 损坏、偏移错误或文件被截断时走这条路。代价是一次全盘扫描，
        但换来"烂文件也能提出文字"。
        """
        self.xref = {}
        self._cache.clear()
        self._objstm_cache.clear()
        self.repaired = True

        for m in _OBJ_RE.finditer(self.data):
            num = int(m.group(1))
            gen = int(m.group(2))
            # 后面的定义覆盖前面的（增量更新场景）
            self.xref[num] = (_IN_FILE, m.start(), gen)

        # 找回 trailer：取最后一个 trailer 字典，或扫描所有 << /Type /Catalog >>
        for m in reversed(list(_TRAILER_RE.finditer(self.data))):
            try:
                parser = Parser(self.data, m.end())
                trailer = parser.parse_object()
                if isinstance(trailer, PdfDict) and "Root" in trailer:
                    self._merge_trailer(trailer)
                    break
            except Exception:
                continue

        if "Root" not in self.trailer:
            root = self._find_catalog()
            if root is not None:
                self.trailer["Root"] = root

        if "Root" not in self.trailer:
            raise DocumentError("无法定位文档根对象（/Root），文件可能已损坏")

        self._expand_objstms()

    def _find_catalog(self) -> PdfRef | None:
        """扫描所有对象，找 ``/Type /Catalog``。"""
        marker = b"/Type"
        for m in re.finditer(re.escape(marker), self.data):
            window = self.data[m.start() : m.start() + 200]
            if b"/Catalog" not in window:
                continue
            # 往上找最近的 "N G obj"
            head = self.data[max(0, m.start() - 2000) : m.start()]
            objs = list(_OBJ_RE.finditer(head))
            if not objs:
                continue
            last = objs[-1]
            return PdfRef(int(last.group(1)), int(last.group(2)))
        return None

    def _expand_objstms(self) -> None:
        """把对象流里的对象登记进 xref 表。

        重建模式下我们只看到 ``N G obj``，看不出哪些对象藏在 ObjStm 里，
        所以主动找一遍 ``/Type /ObjStm`` 并展开。
        """
        for num, (typ, a, _b) in list(self.xref.items()):
            if typ != _IN_FILE:
                continue
            if not self._looks_like_objstm(a):
                continue
            try:
                stream = self.get_object(num)
            except Exception:
                continue
            if not isinstance(stream, PdfStream):
                continue
            try:
                pairs = self._parse_objstm_header(stream)
            except Exception:
                continue
            for index, (onum, _off) in enumerate(pairs):
                self.xref.setdefault(onum, (_IN_OBJSTM, num, index))

    def _looks_like_objstm(self, offset: int) -> bool:
        window = self.data[offset : offset + 512]
        return b"/ObjStm" in window

    # -- 对象访问 --------------------------------------------------------
    def get_object(self, num: int, gen: int = 0) -> Any:
        """取间接对象；不存在返回 :data:`NULL`。"""
        if num in self._cache:
            return self._cache[num]
        if num in self._loading:  # 循环引用保护
            return NULL

        entry = self.xref.get(num)
        if entry is None:
            return NULL

        typ, a, b = entry
        self._loading.add(num)
        try:
            if typ == _IN_FILE:
                value = self._load_at_offset(a)
            elif typ == _IN_OBJSTM:
                value = self._load_from_objstm(a, b)
            else:
                value = NULL
        except Exception:
            value = NULL
        finally:
            self._loading.discard(num)

        self._cache[num] = value
        return value

    def _load_at_offset(self, offset: int) -> Any:
        if not (0 <= offset < len(self.data)):
            return NULL
        for probe in (offset, offset + 1, offset - 1):
            if not (0 <= probe < len(self.data)):
                continue
            try:
                parser = Parser(self.data, probe)
                _, _, value = parser.parse_indirect_object()
                return value
            except ParseError:
                continue
            except Exception:
                continue
        return NULL

    def _decode_objstm(self, stream: PdfStream) -> tuple[bytes, list[tuple[int, int]], int]:
        """解出对象流的 ``(数据, 头部对, First)``。

        头部是 ``/N`` 组 ``objnum offset`` 的 ASCII 文本，``/First`` 指出正文
        起始位置；第 i 个对象就是 ``数据[First + offset_i]`` 处的一个对象。
        """
        n = stream.dict.get("N")
        first = stream.dict.get("First")
        if not isinstance(n, int):
            raise DocumentError("对象流缺少 /N")
        if not isinstance(first, int) or first < 0:
            raise DocumentError("对象流缺少 /First")

        data = filters.decode_stream_data(stream, self._resolve_for_filter)
        tokens = data[:first].split()
        pairs: list[tuple[int, int]] = []
        for i in range(0, min(len(tokens) - 1, n * 2), 2):
            try:
                pairs.append((int(tokens[i]), int(tokens[i + 1])))
            except ValueError:
                break
        return data, pairs, first

    def _parse_objstm_header(self, stream: PdfStream) -> list[tuple[int, int]]:
        """只取对象流的头部对，供交叉引用重建时枚举成员用。"""
        return self._decode_objstm(stream)[1]

    def _load_from_objstm(self, stm_num: int, index: int) -> Any:
        """取对象流里第 ``index`` 个对象（索引即头部对的下标）。"""
        cached = self._objstm_cache.get(stm_num)
        if cached is None:
            stream = self.get_object(stm_num)
            objects: list[Any] = []
            if isinstance(stream, PdfStream):
                try:
                    data, pairs, first = self._decode_objstm(stream)
                    for _onum, off in pairs:
                        try:
                            parser = Parser(data, first + off)
                            objects.append(parser.parse_object())
                        except Exception:
                            objects.append(NULL)
                except Exception:
                    objects = []
            cached = objects
            self._objstm_cache[stm_num] = cached

        if 0 <= index < len(cached):
            return cached[index]
        return NULL

    # -- 递归解析 --------------------------------------------------------
    def resolve(self, obj: Any, depth: int = 0) -> Any:
        """把 :class:`PdfRef` 递归解析成实际对象（带环检测）。"""
        while isinstance(obj, PdfRef) and depth < 32:
            obj = self.get_object(obj.num, obj.gen)
            depth += 1
        return obj

    def resolve_deep(self, obj: Any, depth: int = 0) -> Any:
        """递归解析数组和字典内部的所有引用。"""
        if depth > 24:
            return obj
        obj = self.resolve(obj)
        if isinstance(obj, PdfDict):
            return PdfDict(
                {k: self.resolve_deep(v, depth + 1) for k, v in obj.items()}
            )
        if isinstance(obj, PdfArray):
            return PdfArray(self.resolve_deep(v, depth + 1) for v in obj)
        return obj

    def get_stream_data(self, stream: PdfStream) -> bytes:
        """取流的解码后字节。"""
        return filters.decode_stream_data(stream, self._resolve_for_filter)

    # -- 页面 ------------------------------------------------------------
    @property
    def root(self) -> PdfDict:
        r = self.resolve(self.trailer.get("Root"))
        return r if isinstance(r, PdfDict) else PdfDict()

    _INHERITABLE = ("Resources", "MediaBox", "CropBox", "Rotate")

    @property
    def page_count(self) -> int:
        count = self.resolve(self.root.get("Pages"))
        if isinstance(count, PdfDict):
            n = count.get("Count")
            if isinstance(n, int):
                return n
        return len(list(self.iter_pages()))

    def iter_pages(self) -> Iterator[PdfDict]:
        """按顺序遍历所有页面，自动继承 /Resources /MediaBox /Rotate。"""
        root_pages = self.resolve(self.root.get("Pages"))
        if not isinstance(root_pages, PdfDict):
            return
        yield from self._walk_pages(root_pages, {}, set())

    def _walk_pages(
        self,
        node: Any,
        inherited: dict[str, Any],
        seen: set[int],
    ) -> Iterator[PdfDict]:
        node = self.resolve(node)
        if not isinstance(node, PdfDict):
            return

        # 记录可继承属性
        current = dict(inherited)
        for key in self._INHERITABLE:
            if key in node:
                current[key] = node[key]

        node_type = str(node.get("Type", ""))
        kids = self.resolve(node.get("Kids"))

        if node_type == "Page" or (node_type != "Pages" and kids is None):
            page = PdfDict(node)
            for key, value in current.items():
                page.setdefault(key, value)
            yield page
            return

        if not isinstance(kids, PdfArray):
            return

        for kid in kids:
            # 环检测：同一个对象号不重复展开
            if isinstance(kid, PdfRef):
                if kid.num in seen:
                    continue
                seen = seen | {kid.num}
            yield from self._walk_pages(kid, current, seen)

    @property
    def pages(self) -> list[PdfDict]:
        return list(self.iter_pages())

    def page_content(self, page: PdfDict) -> bytes:
        """拼接页面 ``/Contents`` 的所有内容流。"""
        contents = page.get("Contents")
        if contents is None:
            return b""
        streams = contents if isinstance(contents, (list, tuple)) else [contents]
        chunks: list[bytes] = []
        for item in streams:
            stm = self.resolve(item)
            if isinstance(stm, PdfStream):
                try:
                    chunks.append(self.get_stream_data(stm))
                except Exception:
                    continue
        return b"\n".join(chunks)

    def __repr__(self) -> str:
        return (
            f"<PdfDocument PDF-{self.version} objects={len(self.xref)} "
            f"pages={self.page_count}{' repaired' if self.repaired else ''}>"
        )

"""命令行接口。

设计目标是"零依赖、拿来即用"：不需要 ``pip install`` 任何东西，
``python -m pdftext 文件.pdf`` 就能直接跑。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from . import __version__
from .document import DocumentError, PdfDocument
from .extract import Extractor

__all__ = ["main", "build_parser"]

_PAGE_SEPARATOR = "\f"  # 换页符，与 pdftotext 的习惯一致


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdftext",
        description="零依赖的纯 Python PDF 文本提取器",
        epilog=(
            "示例:\n"
            "  pdftext 文档.pdf\n"
            "  pdftext 文档.pdf -p 1-3,7 -o 输出.txt\n"
            "  pdftext 表格.pdf --layout\n"
            "  pdftext 文档.pdf --json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("files", nargs="+", metavar="PDF", help="要处理的 PDF 文件")
    parser.add_argument(
        "-p", "--pages", default=None, metavar="SPEC",
        help="页码范围，如 1-3,5,8-（默认全部）",
    )
    parser.add_argument(
        "-o", "--output", default=None, metavar="FILE",
        help="输出到文件（默认打印到标准输出）",
    )
    parser.add_argument(
        "-l", "--layout", action="store_true",
        help="近似保留版面（按 x 坐标对齐列），适合表格类 PDF",
    )
    parser.add_argument(
        "-j", "--json", action="store_true",
        help="以 JSON 输出，含每页尺寸、旋转角与行坐标",
    )
    parser.add_argument(
        "-s", "--stats", action="store_true",
        help="只输出统计信息（页数、字符数、是否含文本层）",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="抑制标准错误上的提示"
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"pdftext {__version__}"
    )
    return parser


def _warn(message: str, quiet: bool) -> None:
    if not quiet:
        print(message, file=sys.stderr)


def _configure_output_encoding() -> None:
    """把标准输出与标准错误切到 UTF-8。

    Windows 上 stdout 被**重定向**时的默认编码是 GBK / cp1252（不是 UTF-8），
    此时输出中文或 ``✔`` 这类符号会直接抛 ``UnicodeEncodeError`` 让程序崩掉。
    本项目的 CI 就在 Windows 上踩过这个坑，所以这里显式重配。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def _write_stdout(text: str) -> None:
    """写标准输出，即使上面的重配因为某些环境失败也不会崩。"""
    if not text:
        return
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        buffer = getattr(sys.stdout, "buffer", None)
        if buffer is None:
            sys.stdout.write(text.encode("ascii", "replace").decode("ascii"))
        else:
            buffer.write(text.encode("utf-8", "replace"))


def _extract_one(path: str, args: argparse.Namespace) -> tuple[str, dict | None]:
    """提取单个文件，返回 ``(文本, JSON 结构或 None)``。"""
    doc = PdfDocument.from_file(path)
    if doc.repaired:
        _warn(f"{path}: 交叉引用表损坏，已扫描重建（可能漏页）", args.quiet)

    extractor = Extractor(doc)
    pages = extractor.extract(args.pages)

    if args.stats:
        total_chars = sum(len(p.chars) for p in pages)
        with_text = sum(1 for p in pages if p.has_text)
        lines = [
            f"文件: {path}",
            f"PDF 版本: {doc.version}",
            f"对象数: {len(doc.xref)}",
            f"总页数: {doc.page_count}",
            f"已处理: {len(pages)}",
            f"字符数: {total_chars}",
            f"含文本层的页: {with_text}/{len(pages)}",
        ]
        if with_text == 0 and pages:
            lines.append(
                "提示: 没有任何文本层，这多半是扫描件（纯图片），"
                "需要 OCR 才能取字。"
            )
        return "\n".join(lines), None

    if args.json:
        payload = {
            "file": path,
            "version": doc.version,
            "repaired": doc.repaired,
            "pages": [
                {
                    "page": p.number,
                    "width": round(p.width, 2),
                    "height": round(p.height, 2),
                    "rotation": p.rotation,
                    "chars": len(p.chars),
                    "lines": [
                        {
                            "y": round(line.y, 2),
                            "x": round(line.x, 2),
                            "size": round(line.size, 2),
                            "text": line.text,
                        }
                        for line in p.lines
                    ],
                    "text": p.text,
                }
                for p in pages
            ],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2), payload

    if args.layout:
        return f"\n{_PAGE_SEPARATOR}".join(p.layout_text() for p in pages), None
    return _PAGE_SEPARATOR.join(p.text for p in pages), None


def main(argv: Sequence[str] | None = None) -> int:
    _configure_output_encoding()
    parser = build_parser()
    args = parser.parse_args(argv)

    chunks: list[str] = []
    failed = 0

    for index, path in enumerate(args.files):
        if index:
            chunks.append("")  # 多文件之间空一行
        try:
            text, _ = _extract_one(path, args)
        except FileNotFoundError:
            _warn(f"错误: 找不到文件 {path}", args.quiet)
            failed += 1
            continue
        except DocumentError as exc:
            _warn(f"错误: {path}: {exc}", args.quiet)
            failed += 1
            continue
        except Exception as exc:  # 兜底：不让单个坏文件中断整批处理
            _warn(f"错误: {path}: {type(exc).__name__}: {exc}", args.quiet)
            failed += 1
            continue
        chunks.append(text)

    output = "\n".join(chunks)

    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(output)
                if not output.endswith("\n"):
                    fh.write("\n")
        except OSError as exc:
            _warn(f"错误: 无法写入 {args.output}: {exc}", args.quiet)
            return 1
        if not args.quiet:
            print(f"已写入 {args.output}", file=sys.stderr)
    else:
        _write_stdout(output)
        if output and not output.endswith("\n"):
            _write_stdout("\n")

    return 1 if failed and failed == len(args.files) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

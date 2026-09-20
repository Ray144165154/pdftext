"""命令行接口测试。"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest

from pdfbuilder import make_standard_pdf, multi_page_pdf, one_page_pdf, show, simple_font

from pdftext.cli import build_parser, main


def write_temp(data: bytes, suffix: str = ".pdf") -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return path


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpfiles: list[str] = []

    def tearDown(self):
        for path in self.tmpfiles:
            try:
                os.unlink(path)
            except OSError:
                pass

    def temp(self, data: bytes) -> str:
        path = write_temp(data)
        self.tmpfiles.append(path)
        return path

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()


class TestBasicCli(CliTestCase):
    def test_extract_to_stdout(self):
        path = self.temp(make_standard_pdf([(72, 700, "Hello CLI"), (72, 680, "Second")]))
        code, out, _ = self.run_cli([path])
        self.assertEqual(code, 0)
        self.assertIn("Hello CLI", out)
        self.assertIn("Second", out)

    def test_output_file(self):
        path = self.temp(make_standard_pdf([(72, 700, "ToFile")]))
        out_path = path + ".txt"
        self.tmpfiles.append(out_path)
        code, _, err = self.run_cli([path, "-o", out_path])
        self.assertEqual(code, 0)
        self.assertIn("已写入", err)
        with open(out_path, encoding="utf-8") as fh:
            self.assertIn("ToFile", fh.read())

    def test_page_selection(self):
        def page_setup(text):
            def setup(writer):
                font = simple_font(writer)
                return show(72, 700, text).encode("latin-1"), f"<< /Font << /F1 {font} 0 R >> >>"

            return setup

        # 注意用默认参数绑定 text：闭包在列表推导里会晚绑定，直接引用循环变量
        # 会让三页都输出最后一个值。
        pdf = multi_page_pdf([page_setup(f"PAGE{i}") for i in (1, 2, 3)])
        path = self.temp(pdf)
        code, out, _ = self.run_cli([path, "-p", "2"])
        self.assertEqual(code, 0)
        self.assertIn("PAGE2", out)
        self.assertNotIn("PAGE1", out)
        self.assertNotIn("PAGE3", out)


class TestOutputModes(CliTestCase):
    def test_stats_mode(self):
        path = self.temp(make_standard_pdf([(72, 700, "Stats")]))
        code, out, _ = self.run_cli([path, "--stats"])
        self.assertEqual(code, 0)
        self.assertIn("总页数: 1", out)
        self.assertIn("字符数:", out)

    def test_stats_warns_when_no_text_layer(self):
        """没有文本层时应提示这是扫描件。"""
        def setup(writer):
            return b"q 1 0 0 1 0 0 cm Q\n", "<< >>"

        path = self.temp(one_page_pdf(setup))
        _, out, _ = self.run_cli([path, "--stats"])
        self.assertIn("扫描件", out)

    def test_json_mode(self):
        path = self.temp(make_standard_pdf([(72, 700, "JsonLine")]))
        code, out, _ = self.run_cli([path, "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["version"], "1.7")
        self.assertEqual(len(payload["pages"]), 1)
        self.assertEqual(payload["pages"][0]["page"], 1)
        self.assertIn("JsonLine", payload["pages"][0]["text"])
        self.assertTrue(all("y" in ln for ln in payload["pages"][0]["lines"]))

    def test_layout_mode(self):
        path = self.temp(make_standard_pdf([(72, 700, "A"), (200, 700, "B")]))
        code, out, _ = self.run_cli([path, "--layout"])
        self.assertEqual(code, 0)
        self.assertIn("A", out)
        self.assertIn("B", out)

    def test_quiet_suppresses_stderr(self):
        path = self.temp(make_standard_pdf([(72, 700, "Q")]))
        out_path = path + ".txt"
        self.tmpfiles.append(out_path)
        _, _, err = self.run_cli([path, "-o", out_path, "--quiet"])
        self.assertEqual(err, "")


class TestErrorHandling(CliTestCase):
    def test_missing_file(self):
        code, _, err = self.run_cli(["C:/definitely/not/here.pdf"])
        self.assertEqual(code, 1)
        self.assertIn("找不到文件", err)

    def test_corrupt_file(self):
        path = self.temp(b"not a pdf")
        code, _, err = self.run_cli([path])
        self.assertEqual(code, 1)
        self.assertIn("错误", err)

    def test_partial_failure_continues(self):
        """一个坏文件不应中断整批处理。"""
        good = self.temp(make_standard_pdf([(72, 700, "Good")]))
        bad = self.temp(b"garbage")
        code, out, err = self.run_cli([bad, good])
        self.assertEqual(code, 0)  # 有成功的就不算全败
        self.assertIn("Good", out)
        self.assertIn("错误", err)

    def test_unwritable_output(self):
        path = self.temp(make_standard_pdf([(72, 700, "X")]))
        code, _, err = self.run_cli([path, "-o", "C:/definitely/not/a/dir/out.txt"])
        self.assertEqual(code, 1)
        self.assertIn("无法写入", err)


class TestArgumentParsing(unittest.TestCase):
    def test_version_flag_exits(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["--version"])

    def test_requires_input_file(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args([])

    def test_multiple_files_accepted(self):
        args = build_parser().parse_args(["a.pdf", "b.pdf"])
        self.assertEqual(args.files, ["a.pdf", "b.pdf"])

    def test_short_flags(self):
        args = build_parser().parse_args(["-p", "1-3", "-l", "-o", "x.txt", "f.pdf"])
        self.assertEqual(args.pages, "1-3")
        self.assertTrue(args.layout)
        self.assertEqual(args.output, "x.txt")


if __name__ == "__main__":
    unittest.main()

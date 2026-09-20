"""词法分析器与对象解析器的单元测试。"""

from __future__ import annotations

import unittest

from pdftext.objects import (
    NULL,
    Lexer,
    ParseError,
    Parser,
    PdfArray,
    PdfDict,
    PdfName,
    PdfRef,
    PdfStream,
    tokenize,
)


class TestLexer(unittest.TestCase):
    def test_integers_and_reals(self):
        self.assertEqual(Lexer(b"123").next(), ("num", 123))
        self.assertEqual(Lexer(b"-4.25").next(), ("num", -4.25))
        self.assertEqual(Lexer(b".5").next(), ("num", 0.5))
        self.assertEqual(Lexer(b"5.").next(), ("num", 5.0))
        self.assertEqual(Lexer(b"+7").next(), ("num", 7))

    def test_number_then_operator(self):
        lx = Lexer(b"1 0 0 1 72 700 Tm")
        kinds = [lx.next()[:1] for _ in range(7)]
        self.assertEqual(kinds, [("num",)] * 6 + [("kw",)])

    def test_names(self):
        self.assertEqual(Lexer(b"/Type").next(), ("name", "Type"))
        # #XX 转义：/A#20B 表示带空格的名称
        self.assertEqual(Lexer(b"/A#20B").next(), ("name", "A B"))
        self.assertEqual(Lexer(b"/").next(), ("name", ""))

    def test_literal_string(self):
        self.assertEqual(Lexer(b"(hello)").next(), ("str", b"hello"))
        # 转义序列
        self.assertEqual(Lexer(b"(a\\tb)").next(), ("str", b"a\tb"))
        self.assertEqual(Lexer(b"(a\\(b\\)c)").next(), ("str", b"a(b)c"))
        # 括号嵌套不算结束
        self.assertEqual(Lexer(b"(a(nested)b)").next(), ("str", b"a(nested)b"))
        # 八进制转义 \101 = 'A'
        self.assertEqual(Lexer(b"(\\101)").next(), ("str", b"A"))
        # 反斜杠 + 换行 = 行继续
        self.assertEqual(Lexer(b"(ab\\\ncd)").next(), ("str", b"abcd"))

    def test_hex_string(self):
        self.assertEqual(Lexer(b"<48656C6C6F>").next(), ("hexstr", b"Hello"))
        # 奇数位补 0：48656C6C6F7 -> 48656C6C6F70
        self.assertEqual(Lexer(b"<48656C6C6F7>").next(), ("hexstr", b"Hellop"))
        # 内部空白忽略
        self.assertEqual(Lexer(b"<48 65 6C>").next(), ("hexstr", b"Hel"))
        self.assertEqual(Lexer(b"<>").next(), ("hexstr", b""))

    def test_delimiters_and_comments(self):
        self.assertEqual(Lexer(b"<<").next(), ("dict_open", None))
        self.assertEqual(Lexer(b">>").next(), ("dict_close", None))
        self.assertEqual(Lexer(b"[").next(), ("arr_open", None))
        self.assertEqual(Lexer(b"]").next(), ("arr_close", None))
        self.assertEqual(Lexer(b"% comment here\n42").next(), ("num", 42))

    def test_mark_and_reset(self):
        lx = Lexer(b"1 2 3")
        lx.next()
        mark = lx.mark()
        self.assertEqual(lx.next(), ("num", 2))
        lx.reset(mark)
        self.assertEqual(lx.next(), ("num", 2))

    def test_tokenize_helper(self):
        tokens = list(tokenize(b"/A 1 (x) [ ]"))
        self.assertEqual(
            [k for k, _ in tokens],
            ["name", "num", "str", "arr_open", "arr_close"],
        )


class TestParser(unittest.TestCase):
    def parse(self, data: bytes):
        return Parser(data).parse_object()

    def test_scalars(self):
        self.assertEqual(self.parse(b"42"), 42)
        self.assertEqual(self.parse(b"true"), True)
        self.assertEqual(self.parse(b"false"), False)
        self.assertIs(self.parse(b"null"), NULL)
        self.assertEqual(self.parse(b"/Name"), PdfName("Name"))
        self.assertEqual(self.parse(b"(hi)"), b"hi")

    def test_array(self):
        self.assertEqual(self.parse(b"[1 2 3]"), [1, 2, 3])
        arr = self.parse(b"[1 [2 3] /X (s)]")
        self.assertIsInstance(arr, PdfArray)
        self.assertEqual(arr[1], [2, 3])
        self.assertEqual(arr[2], PdfName("X"))
        self.assertEqual(arr[3], b"s")

    def test_dictionary(self):
        d = self.parse(b"<< /Type /Page /Count 3 /Kids [] >>")
        self.assertIsInstance(d, PdfDict)
        self.assertEqual(d["Type"], PdfName("Page"))
        self.assertEqual(d["Count"], 3)
        self.assertEqual(d["Kids"], [])
        self.assertEqual(d.get_int("Count"), 3)

    def test_indirect_reference(self):
        ref = self.parse(b"12 0 R")
        self.assertEqual(ref, PdfRef(12, 0))
        # 相邻数字不应被误判成引用
        self.assertEqual(self.parse(b"12"), 12)
        # 数字后面不是 R，要能正确回溯
        arr = self.parse(b"[1 2]")
        self.assertEqual(arr, [1, 2])

    def test_nested_dict_in_array(self):
        value = self.parse(b"[<< /A 1 >> << /B [2 3] >>]")
        self.assertEqual(value[0]["A"], 1)
        self.assertEqual(value[1]["B"], [2, 3])

    def test_indirect_object(self):
        num, gen, value = Parser(b"7 0 obj\n42\nendobj").parse_indirect_object()
        self.assertEqual((num, gen), (7, 0))
        self.assertEqual(value, 42)

    def test_stream_with_length(self):
        data = b"7 0 obj\n<< /Length 5 >>\nstream\nHELLO\nendstream\nendobj"
        num, gen, value = Parser(data).parse_indirect_object()
        self.assertEqual(num, 7)
        self.assertIsInstance(value, PdfStream)
        self.assertEqual(value.raw, b"HELLO")

    def test_stream_with_wrong_length_falls_back_to_search(self):
        # /Length 声明错了，解析器应当退回搜索 endstream
        data = b"7 0 obj\n<< /Length 999 >>\nstream\nHELLO\nendstream\nendobj"
        _, _, value = Parser(data).parse_indirect_object()
        self.assertIsInstance(value, PdfStream)
        self.assertEqual(value.raw, b"HELLO")

    def test_error_on_unclosed_dict(self):
        with self.assertRaises(ParseError):
            self.parse(b"<< /A 1")


if __name__ == "__main__":
    unittest.main()

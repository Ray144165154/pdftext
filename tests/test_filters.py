"""流过滤器（解码器）的单元测试。"""

from __future__ import annotations

import unittest
import zlib

from pdftext.filters import (
    FilterError,
    apply_predictor,
    ascii85_decode,
    ascii_hex_decode,
    decode_stream_data,
    flate_decode,
    lzw_decode,
    run_length_decode,
)
from pdftext.objects import PdfDict, PdfStream


class TestFlate(unittest.TestCase):
    def test_roundtrip(self):
        original = b"hello pdf world" * 20
        self.assertEqual(flate_decode(zlib.compress(original)), original)

    def test_truncated_stream_still_decodes(self):
        original = b"A" * 500
        compressed = zlib.compress(original)
        # 砍掉校验尾部，宽容模式应当仍能解出大部分数据
        partial = flate_decode(compressed[:-4])
        self.assertTrue(partial.startswith(b"AAA"))

    def test_empty(self):
        self.assertEqual(flate_decode(b""), b"")

    def test_raw_deflate(self):
        compressor = zlib.compressobj(wbits=-15)
        raw = compressor.compress(b"raw deflate data") + compressor.flush()
        self.assertEqual(flate_decode(raw), b"raw deflate data")

    def test_garbage_raises(self):
        with self.assertRaises(FilterError):
            flate_decode(b"\xff\xfe\xfd\xfc" * 8)


class TestPredictors(unittest.TestCase):
    def test_png_up_predictor(self):
        # 两行 4 字节，filter type 2 = Up
        encoded = bytes([2, 1, 2, 3, 4, 2, 4, 4, 4, 4])
        decoded = apply_predictor(
            encoded, predictor=15, colors=1, bits_per_component=8, columns=4
        )
        self.assertEqual(decoded, bytes([1, 2, 3, 4, 5, 6, 7, 8]))

    def test_png_none_predictor(self):
        encoded = bytes([0, 1, 2, 3, 4])
        decoded = apply_predictor(
            encoded, predictor=12, colors=1, bits_per_component=8, columns=4
        )
        self.assertEqual(decoded, bytes([1, 2, 3, 4]))

    def test_png_sub_predictor(self):
        # Sub：每个字节减去左边 bpp 个字节（bpp=1）
        encoded = bytes([1, 10, 5, 5, 5])
        decoded = apply_predictor(
            encoded, predictor=12, colors=1, bits_per_component=8, columns=4
        )
        self.assertEqual(decoded, bytes([10, 15, 20, 25]))

    def test_tiff_predictor(self):
        # TIFF predictor 2：每个字节累加左边 colors 个字节
        encoded = bytes([10, 5, 5, 5])
        decoded = apply_predictor(
            encoded, predictor=2, colors=1, bits_per_component=8, columns=4
        )
        self.assertEqual(decoded, bytes([10, 15, 20, 25]))

    def test_predictor_1_is_identity(self):
        data = b"\x01\x02\x03"
        self.assertEqual(apply_predictor(data, predictor=1), data)


class TestSimpleFilters(unittest.TestCase):
    def test_ascii_hex(self):
        self.assertEqual(ascii_hex_decode(b"48656C6C6F>"), b"Hello")
        self.assertEqual(ascii_hex_decode(b"48 65 6C 6C 6F"), b"Hello")
        # 奇数位补 0
        self.assertEqual(ascii_hex_decode(b"48656C6C6F7>"), b"Hellop")

    def test_ascii85(self):
        # 5 个 base-85 数字 -> 4 字节大端整数
        self.assertEqual(ascii85_decode(b"<~!<N?+~>"), b"\x01\x02\x03\x04")
        # 'z' 是四个零字节的缩写
        self.assertEqual(ascii85_decode(b"<~z~>"), b"\x00\x00\x00\x00")
        # 末尾不足 5 个数字按补位处理
        self.assertEqual(len(ascii85_decode(b"<~!!~>")), 1)

    def test_run_length(self):
        # 长度 2 表示后面 3 个字节是字面量
        self.assertEqual(run_length_decode(b"\x02ABC\x80"), b"ABC")
        # 254 表示 257-254 = 3 个重复字节
        self.assertEqual(run_length_decode(b"\xfe\x41\x80"), b"AAA")

    def test_lzw(self):
        # 手工构造：256(清表) / 65('A') / 257(结束)，各 9 位
        stream = bytes([0x80, 0x10, 0x60, 0x20])
        self.assertEqual(lzw_decode(stream), b"A")


class TestDecodeStreamData(unittest.TestCase):
    def test_no_filter(self):
        stream = PdfStream(PdfDict(), b"raw bytes")
        self.assertEqual(decode_stream_data(stream), b"raw bytes")

    def test_flate_chain(self):
        payload = zlib.compress(b"content")
        stream = PdfStream(PdfDict({"Filter": "FlateDecode"}), payload)
        self.assertEqual(decode_stream_data(stream), b"content")

    def test_filter_array_with_predictor(self):
        table = bytes([2, 1, 2, 3, 4, 2, 4, 4, 4, 4])
        payload = zlib.compress(table)
        stream = PdfStream(
            PdfDict(
                {
                    "Filter": ["FlateDecode"],
                    "DecodeParms": [
                        {"Predictor": 15, "Columns": 4, "Colors": 1, "BitsPerComponent": 8}
                    ],
                }
            ),
            payload,
        )
        self.assertEqual(decode_stream_data(stream), bytes([1, 2, 3, 4, 5, 6, 7, 8]))

    def test_image_filter_passes_through(self):
        stream = PdfStream(PdfDict({"Filter": "DCTDecode"}), b"\xff\xd8jpegdata")
        self.assertEqual(decode_stream_data(stream), b"\xff\xd8jpegdata")


if __name__ == "__main__":
    unittest.main()

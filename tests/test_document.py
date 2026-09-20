"""文档层测试：交叉引用、对象流、页面树、旋转、容错重建。"""

from __future__ import annotations

import re
import unittest

from pdfbuilder import (
    PdfWriter,
    make_standard_pdf,
    multi_page_pdf,
    one_page_pdf,
    show,
    simple_font,
)

from pdftext import Extractor, PdfDocument
from pdftext.document import DocumentError
from pdftext.objects import PdfDict, PdfName, PdfRef


def standard_setup(text: str = "X"):
    def setup(writer):
        font = simple_font(writer)
        return show(72, 700, text).encode("latin-1"), f"<< /Font << /F1 {font} 0 R >> >>"

    return setup


class TestXrefParsing(unittest.TestCase):
    def test_traditional_xref_table(self):
        pdf = make_standard_pdf([(72, 700, "Hello")])
        doc = PdfDocument.from_bytes(pdf)
        self.assertFalse(doc.repaired)
        self.assertEqual(doc.version, "1.7")
        self.assertEqual(doc.page_count, 1)
        self.assertIsInstance(doc.root, PdfDict)
        self.assertEqual(doc.root.get("Type"), PdfName("Catalog"))

    def test_xref_stream(self):
        pdf = make_standard_pdf([(72, 700, "Hello")], xref_stream=True)
        doc = PdfDocument.from_bytes(pdf)
        self.assertFalse(doc.repaired)
        self.assertEqual(
            "".join(c.text for c in Extractor(doc).extract_page(0).chars), "Hello"
        )

    def test_object_stream(self):
        """对象流（ObjStm）里的对象没有独立实体，只能靠 type-2 引用找到。"""
        writer = PdfWriter()
        font_num = simple_font(writer)
        content_num = writer.add_stream(show(72, 700, "InObjStm").encode("latin-1"))

        page_num = writer.reserve()
        pages_num = writer.reserve()
        root_num = writer.reserve()
        writer.put(
            page_num,
            (
                f"<< /Type /Page /Parent {pages_num} 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {font_num} 0 R >> >> "
                f"/Contents {content_num} 0 R >>"
            ).encode("latin-1"),
        )
        writer.put(
            pages_num,
            f"<< /Type /Pages /Kids [{page_num} 0 R] /Count 1 >>".encode("latin-1"),
        )
        writer.put(root_num, f"<< /Type /Catalog /Pages {pages_num} 0 R >>".encode("latin-1"))

        pdf = writer.build_compressed(root_num, [page_num, pages_num, root_num])
        doc = PdfDocument.from_bytes(pdf)
        self.assertFalse(doc.repaired)
        self.assertEqual(doc.page_count, 1)
        self.assertEqual(
            "".join(c.text for c in Extractor(doc).extract_page(0).chars), "InObjStm"
        )


class TestRepair(unittest.TestCase):
    def test_broken_startxref_triggers_rebuild(self):
        """startxref 指错位置时，应扫描重建而不是直接失败。"""
        pdf = make_standard_pdf([(72, 700, "Repaired")])
        corrupted = re.sub(rb"startxref\s+\d+", b"startxref\n999", pdf)
        doc = PdfDocument.from_bytes(corrupted)
        self.assertTrue(doc.repaired)
        self.assertEqual(doc.page_count, 1)
        self.assertEqual(
            "".join(c.text for c in Extractor(doc).extract_page(0).chars), "Repaired"
        )

    def test_missing_startxref_triggers_rebuild(self):
        pdf = make_standard_pdf([(72, 700, "NoXref")])
        corrupted = pdf.replace(b"startxref", b"xxxxxxxxx")
        doc = PdfDocument.from_bytes(corrupted)
        self.assertTrue(doc.repaired)
        self.assertEqual(
            "".join(c.text for c in Extractor(doc).extract_page(0).chars), "NoXref"
        )

    def test_completely_broken_file_raises(self):
        with self.assertRaises(DocumentError):
            PdfDocument.from_bytes(b"this is not a pdf at all")

    def test_encrypted_detected(self):
        pdf = make_standard_pdf([(72, 700, "Secret")])
        encrypted = pdf.replace(b"/Size", b"/Encrypt 99 0 R /Size", 1)
        with self.assertRaises(DocumentError) as ctx:
            PdfDocument.from_bytes(encrypted)
        self.assertIn("加密", str(ctx.exception))


class TestPageTree(unittest.TestCase):
    def test_multiple_pages(self):
        pdf = multi_page_pdf([standard_setup(t) for t in ("P1", "P2", "P3")])
        doc = PdfDocument.from_bytes(pdf)
        self.assertEqual(doc.page_count, 3)
        self.assertEqual(len(doc.pages), 3)
        texts = [p.text for p in Extractor(doc).extract()]
        self.assertEqual(texts, ["P1", "P2", "P3"])

    def test_page_number_is_one_based(self):
        pdf = multi_page_pdf([standard_setup("A"), standard_setup("B")])
        pages = Extractor(PdfDocument.from_bytes(pdf)).extract()
        self.assertEqual([p.number for p in pages], [1, 2])

    def test_page_spec_selection(self):
        pdf = multi_page_pdf([standard_setup(f"P{i}") for i in range(1, 6)])
        extractor = Extractor(PdfDocument.from_bytes(pdf))
        self.assertEqual([p.text for p in extractor.extract("1-2")], ["P1", "P2"])
        self.assertEqual([p.text for p in extractor.extract("4")], ["P4"])
        self.assertEqual([p.text for p in extractor.extract("2,4")], ["P2", "P4"])
        self.assertEqual([p.text for p in extractor.extract("3-")], ["P3", "P4", "P5"])
        self.assertEqual(len(extractor.extract(None)), 5)

    def test_page_spec_deduplicates(self):
        pdf = multi_page_pdf([standard_setup(f"P{i}") for i in range(1, 4)])
        extractor = Extractor(PdfDocument.from_bytes(pdf))
        self.assertEqual([p.text for p in extractor.extract("1,1,2")], ["P1", "P2"])

    def test_page_spec_out_of_range_ignored(self):
        pdf = multi_page_pdf([standard_setup("P1")])
        extractor = Extractor(PdfDocument.from_bytes(pdf))
        self.assertEqual(len(extractor.extract("9")), 0)

    def test_extract_page_out_of_range_raises(self):
        pdf = multi_page_pdf([standard_setup("P1")])
        with self.assertRaises(IndexError):
            Extractor(PdfDocument.from_bytes(pdf)).extract_page(5)

    def test_resources_inherited_from_pages_node(self):
        """/Resources 挂在 /Pages 上时，子页面要能继承到。"""
        writer = PdfWriter()
        font_num = simple_font(writer)
        content_num = writer.add_stream(show(72, 700, "Inherited").encode("latin-1"))
        pages_num = writer.reserve()
        root_num = writer.reserve()
        page_num = writer.reserve()
        writer.put(
            page_num,
            (
                f"<< /Type /Page /Parent {pages_num} 0 R /MediaBox [0 0 612 792] "
                f"/Contents {content_num} 0 R >>"
            ).encode("latin-1"),
        )
        writer.put(
            pages_num,
            (
                f"<< /Type /Pages /Kids [{page_num} 0 R] /Count 1 "
                f"/Resources << /Font << /F1 {font_num} 0 R >> >> >>"
            ).encode("latin-1"),
        )
        writer.put(root_num, f"<< /Type /Catalog /Pages {pages_num} 0 R >>".encode("latin-1"))

        doc = PdfDocument.from_bytes(writer.build(root_num))
        self.assertEqual(
            "".join(c.text for c in Extractor(doc).extract_page(0).chars), "Inherited"
        )

    def test_cycle_in_kids_does_not_hang(self):
        """恶意/损坏文件里 Kids 成环，遍历要有环检测。"""
        writer = PdfWriter()
        pages_num = writer.reserve()
        root_num = writer.reserve()
        writer.put(
            pages_num,
            f"<< /Type /Pages /Kids [{pages_num} 0 R] /Count 1 >>".encode("latin-1"),
        )
        writer.put(root_num, f"<< /Type /Catalog /Pages {pages_num} 0 R >>".encode("latin-1"))
        doc = PdfDocument.from_bytes(writer.build(root_num))
        self.assertEqual(len(doc.pages), 0)  # 不挂死即可


class TestRotation(unittest.TestCase):
    def test_rotate_90(self):
        pdf = one_page_pdf(standard_setup("R"), rotate=90)
        page = Extractor(PdfDocument.from_bytes(pdf)).extract_page(0)
        self.assertEqual(page.rotation, 90)
        self.assertAlmostEqual(page.width, 792.0, places=2)
        self.assertAlmostEqual(page.height, 612.0, places=2)
        # 原 (72, 700) 顺时针转 90° 后 -> (792-700, 72) = (92, 72)
        self.assertAlmostEqual(page.chars[0].x, 92.0, places=2)
        self.assertAlmostEqual(page.chars[0].y, 72.0, places=2)

    def test_rotate_180(self):
        pdf = one_page_pdf(standard_setup("R"), rotate=180)
        page = Extractor(PdfDocument.from_bytes(pdf)).extract_page(0)
        self.assertEqual(page.rotation, 180)
        self.assertAlmostEqual(page.width, 612.0, places=2)
        self.assertAlmostEqual(page.height, 792.0, places=2)
        self.assertAlmostEqual(page.chars[0].x, 612.0 - 72.0, places=2)
        self.assertAlmostEqual(page.chars[0].y, 792.0 - 700.0, places=2)

    def test_rotate_270(self):
        pdf = one_page_pdf(standard_setup("R"), rotate=270)
        page = Extractor(PdfDocument.from_bytes(pdf)).extract_page(0)
        self.assertEqual(page.rotation, 270)
        self.assertAlmostEqual(page.width, 792.0, places=2)
        self.assertAlmostEqual(page.height, 612.0, places=2)

    def test_media_box_offset_normalized(self):
        """MediaBox 左下角不在原点时，坐标要平移到原点。"""
        pdf = one_page_pdf(standard_setup("O"), mediabox=(10.0, 20.0, 622.0, 812.0))
        page = Extractor(PdfDocument.from_bytes(pdf)).extract_page(0)
        self.assertAlmostEqual(page.chars[0].x, 62.0, places=2)
        self.assertAlmostEqual(page.chars[0].y, 680.0, places=2)

    def test_missing_mediabox_uses_letter_default(self):
        writer = PdfWriter()
        font_num = simple_font(writer)
        content_num = writer.add_stream(show(72, 700, "NoBox").encode("latin-1"))
        pages_num = writer.reserve()
        root_num = writer.reserve()
        page_num = writer.reserve()
        writer.put(
            page_num,
            (
                f"<< /Type /Page /Parent {pages_num} 0 R "
                f"/Resources << /Font << /F1 {font_num} 0 R >> >> "
                f"/Contents {content_num} 0 R >>"
            ).encode("latin-1"),
        )
        writer.put(pages_num, f"<< /Type /Pages /Kids [{page_num} 0 R] /Count 1 >>".encode("latin-1"))
        writer.put(root_num, f"<< /Type /Catalog /Pages {pages_num} 0 R >>".encode("latin-1"))
        page = Extractor(PdfDocument.from_bytes(writer.build(root_num))).extract_page(0)
        self.assertAlmostEqual(page.width, 612.0, places=2)
        self.assertAlmostEqual(page.height, 792.0, places=2)


class TestObjectAccess(unittest.TestCase):
    def test_resolve_reference(self):
        pdf = make_standard_pdf([(72, 700, "X")])
        doc = PdfDocument.from_bytes(pdf)
        root_ref = doc.trailer.get("Root")
        self.assertIsInstance(root_ref, PdfRef)
        self.assertIsInstance(doc.resolve(root_ref), PdfDict)

    def test_missing_object_returns_null(self):
        pdf = make_standard_pdf([(72, 700, "X")])
        doc = PdfDocument.from_bytes(pdf)
        from pdftext.objects import NULL

        self.assertIs(doc.get_object(99999), NULL)

    def test_resolve_deep(self):
        pdf = make_standard_pdf([(72, 700, "X")])
        doc = PdfDocument.from_bytes(pdf)
        resolved = doc.resolve_deep(doc.trailer)
        self.assertIsInstance(resolved, PdfDict)
        self.assertNotIsInstance(resolved.get("Root"), PdfRef)

    def test_repr(self):
        pdf = make_standard_pdf([(72, 700, "X")])
        self.assertIn("PdfDocument", repr(PdfDocument.from_bytes(pdf)))


if __name__ == "__main__":
    unittest.main()

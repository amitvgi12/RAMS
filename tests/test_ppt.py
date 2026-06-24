"""Structural validity of the stdlib .pptx writer (rams.ppt)."""
import xml.dom.minidom as minidom
import zipfile
import io
import unittest

from rams.ppt import pptx_bytes


class TestPptx(unittest.TestCase):
    def _deck(self):
        return pptx_bytes([
            {"title": "Title", "subtitle": "A subtitle"},
            {"title": "Bullets", "bullets": ["one", "two", ("nested", 1)]},
            {"title": "Special <chars> & \"quotes\"", "bullets": ["a < b & c > d"]},
        ])

    def test_valid_zip_and_xml(self):
        z = zipfile.ZipFile(io.BytesIO(self._deck()))
        self.assertIsNone(z.testzip())
        for name in z.namelist():
            if name.endswith((".xml", ".rels")):
                minidom.parseString(z.read(name))  # raises on malformed XML

    def test_required_parts_present(self):
        z = zipfile.ZipFile(io.BytesIO(self._deck()))
        names = set(z.namelist())
        for required in (
            "[Content_Types].xml", "_rels/.rels",
            "ppt/presentation.xml", "ppt/_rels/presentation.xml.rels",
            "ppt/theme/theme1.xml",
            "ppt/slideMasters/slideMaster1.xml",
            "ppt/slideLayouts/slideLayout1.xml",
        ):
            self.assertIn(required, names)

    def test_one_part_and_override_per_slide(self):
        z = zipfile.ZipFile(io.BytesIO(self._deck()))
        names = z.namelist()
        slides = [n for n in names if n.startswith("ppt/slides/slide") and n.endswith(".xml")]
        self.assertEqual(len(slides), 3)
        ct = z.read("[Content_Types].xml").decode()
        for i in (1, 2, 3):
            self.assertIn(f"/ppt/slides/slide{i}.xml", ct)            # content-type override
            self.assertIn(f"ppt/slides/_rels/slide{i}.xml.rels", names)  # slide -> layout rel

    def test_text_is_escaped(self):
        z = zipfile.ZipFile(io.BytesIO(self._deck()))
        s3 = z.read("ppt/slides/slide3.xml").decode()
        self.assertIn("&amp;", s3)
        self.assertIn("&lt;", s3)
        self.assertNotIn("<chars>", s3)


if __name__ == "__main__":
    unittest.main()

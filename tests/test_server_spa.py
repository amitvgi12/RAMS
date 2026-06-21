"""
Guards for the embedded single-page app.

A single JavaScript syntax error in the inlined SPA silently breaks *every* tab
and button (the whole <script> fails to parse). These tests catch the common
cause -- a malformed string literal -- and, when Node is available, syntax-check
the extracted script directly.
"""
import re
import shutil
import subprocess
import tempfile
import unittest

from rams import server


def _extract_js() -> str:
    m = re.search(r"<script>(.*)</script>", server.INDEX_HTML, re.DOTALL)
    assert m, "no <script> block found in INDEX_HTML"
    return m.group(1)


class TestEmbeddedSpa(unittest.TestCase):
    def test_no_backslash_backslash_quote(self):
        # `\\'` inside a single-quoted JS string terminates it early -> SyntaxError
        # that takes down the entire SPA. It must never appear in the page.
        self.assertNotIn(r"\\'", server.INDEX_HTML)

    def test_handlers_referenced_by_buttons_exist(self):
        js = _extract_js()
        for fn in ("runForecast", "runNetwork", "importNetwork", "importSegment",
                   "runResidual", "runCalibrate", "calHint", "toggleHdm4",
                   "runDesign", "runPBMC"):
            self.assertIn(f"function {fn}", js, f"missing handler {fn}")

    def test_new_tabs_and_routes_present(self):
        self.assertIn('data-tab="dsn"', server.INDEX_HTML)  # Design & PBMC tab
        self.assertIn("/api/design", server._ROUTES)
        self.assertIn("/api/pbmc", server._ROUTES)

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_js_parses_with_node(self):
        js = _extract_js()
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
            path = fh.name
        proc = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()

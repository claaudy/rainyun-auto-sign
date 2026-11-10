"""Offline browser regressions for the official Rainyun RYS algorithm."""
import sys
import unittest
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rainyun_signin


class RysHeaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.set_content("<html><body>RYS regression fixture</body></html>")
        self.page.evaluate("Date.now = () => 1700000000120")
        self.old_timeout = rainyun_signin.DEFAULT_TIMEOUT_MS
        rainyun_signin.DEFAULT_TIMEOUT_MS = 250

    def tearDown(self):
        rainyun_signin.DEFAULT_TIMEOUT_MS = self.old_timeout
        self.page.close()

    def test_numeric_zero_uses_official_addition_without_waiting_for_string(self):
        self.page.evaluate("window.fea = 0")
        expected = self.page.evaluate("String(window.fea + Date.now() / 1000)")
        try:
            actual = rainyun_signin.build_rys_header(self.page)
        except PlaywrightTimeoutError:
            self.fail("Numeric fea=0 is valid in the official frontend; it must not time out")
        self.assertEqual(actual, expected)

    def test_string_fingerprint_keeps_javascript_timestamp_format(self):
        self.page.evaluate("window.fea = 'fixture-prefix-'")
        expected = self.page.evaluate("String(window.fea + Date.now() / 1000)")
        self.assertEqual(rainyun_signin.build_rys_header(self.page), expected)

    def test_finite_numbers_use_addition_not_concatenation(self):
        for value in (1, 1.5, -2):
            with self.subTest(value=value):
                self.page.evaluate("value => { window.fea = value; }", value)
                expected = self.page.evaluate("String(window.fea + Date.now() / 1000)")
                self.assertEqual(rainyun_signin.build_rys_header(self.page), expected)

    def test_unready_fingerprints_still_time_out(self):
        for expression in ("undefined", "null", "false", "''", "NaN", "Infinity", "({})"):
            with self.subTest(expression=expression):
                self.page.evaluate(f"window.fea = {expression}")
                with self.assertRaises(PlaywrightTimeoutError):
                    rainyun_signin.build_rys_header(self.page)

    def test_waits_for_delayed_numeric_initialization(self):
        self.page.evaluate("delete window.fea; setTimeout(() => { window.fea = 0; }, 40)")
        actual = rainyun_signin.build_rys_header(self.page)
        expected = self.page.evaluate("String(window.fea + Date.now() / 1000)")
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()

"""Tests for scraper.py. Run from the project folder with:  python -m unittest discover -s tests -v"""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scraper  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def read_fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return fh.read()


class ParseTests(unittest.TestCase):
    def test_directory_page_one(self):
        links, next_href, claimed = scraper.parse_directory(read_fixture("communities_page1.html"))
        self.assertEqual(len(links), 16)
        self.assertEqual(links[0][0], "bellhaven-of-maplewood")
        self.assertEqual(next_href, "/communities?page=2")
        self.assertEqual(claimed, 34)

    def test_detail_page_findlay(self):
        loc = scraper.parse_detail(
            read_fixture("location_findlay.html"),
            "https://x.test/communities/bellhaven-meadows-of-findlay")
        self.assertEqual(loc["name"], "Bellhaven Meadows of Findlay")
        self.assertEqual(loc["street"], "1800 N Blanchard St")
        self.assertEqual((loc["city"], loc["state"], loc["zip"]), ("Findlay", "OH", "45840"))
        self.assertEqual(loc["care_offerings"], ["Assisted Living", "Memory Support"])
        self.assertEqual(loc["administrator"], "Sam Pruitt")
        self.assertEqual(loc["notices"], [])
        self.assertEqual(loc["warnings"], [])
        self.assertEqual(loc["slug"], "bellhaven-meadows-of-findlay")

    def test_notice_banner_is_captured(self):
        html = read_fixture("location_findlay.html").replace(
            '<dl class="detail">',
            '<div class="notice">This community is now operated by Maple Group.</div><dl class="detail">')
        loc = scraper.parse_detail(html, "https://x.test/communities/a")
        self.assertEqual(loc["notices"], ["This community is now operated by Maple Group."])

    def test_odd_address_gives_warning_not_crash(self):
        html = read_fixture("location_findlay.html").replace(
            "1800 N Blanchard St<br>Findlay, OH 45840", "somewhere vague")
        loc = scraper.parse_detail(html, "https://x.test/communities/a")
        self.assertTrue(any("address not understood" in w for w in loc["warnings"]))
        self.assertEqual(loc["zip"], "")

    def test_hash_changes_when_page_changes(self):
        a = scraper.parse_detail(read_fixture("location_findlay.html"), "https://x.test/communities/a")
        b = scraper.parse_detail(read_fixture("location_findlay.html").replace("45840", "45841"),
                                 "https://x.test/communities/a")
        self.assertNotEqual(scraper.content_hash(a), scraper.content_hash(b))
        self.assertEqual(scraper.content_hash(a), scraper.content_hash(dict(a)))


class FakeSite(BaseHTTPRequestHandler):
    """A pretend Bellhaven website: 3 directory pages (16+16+2) and 35 community pages."""
    DETAIL = read_fixture("location_findlay.html")
    DIRECTORY = read_fixture("communities_page1.html")
    SLUGS = [f"bellhaven-test-{i:02d}" for i in range(34)]

    def log_message(self, *args):
        pass

    def _send(self, code, body):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def directory(self, page):
        chunks = {1: self.SLUGS[0:16], 2: self.SLUGS[16:32], 3: self.SLUGS[32:34]}[page]
        cards = "".join(
            f'<div class="card"><h3><a href="/communities/{s}">{s}</a></h3></div>' for s in chunks)
        nxt = f'<a href="/communities?page={page + 1}">Next &rarr;</a>' if page < 3 else ""
        return (f"<html><body><h1>Our Communities</h1><p>Page {page} of 3 · 34 communities listed</p>"
                f"{cards}<div class='pager'>{nxt}</div></body></html>")

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/":
            self._send(200, '<html><body><p>we serve 35 communities</p>'
                            '<a href="/communities/bellhaven-meadows-of-findlay">Findlay</a>'
                            '<a href="/communities">dir</a></body></html>')
        elif path == "/about":
            self._send(200, "<html><body>about</body></html>")
        elif path == "/communities":
            page = int(query.split("=")[1]) if query.startswith("page=") else 1
            self._send(200, self.directory(page))
        elif path.startswith("/communities/"):
            slug = path.rsplit("/", 1)[1]
            if slug in self.SLUGS or slug == "bellhaven-meadows-of-findlay":
                body = self.DETAIL.replace("Bellhaven Meadows of Findlay", slug)
                self._send(200, body)
            else:
                self._send(404, "not found")
        else:
            self._send(404, "not found")


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeSite)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_full_scrape_follows_pages_and_finds_homepage_only_community(self):
        result, snapshot = scraper.scrape(f"http://127.0.0.1:{self.port}", delay=0, log=lambda *_: None)
        meta = result["meta"]
        self.assertEqual(meta["directory_pages"], 3)
        self.assertEqual(meta["directory_slugs"], 34)
        self.assertEqual(len(result["locations"]), 35)
        self.assertEqual(meta["homepage_only_slugs"], ["bellhaven-meadows-of-findlay"])
        self.assertEqual(meta["fetch_errors"], [])
        self.assertEqual(meta["directory_claimed_total"], 34)
        self.assertEqual(meta["homepage_claimed_total"], 35)

    def test_main_writes_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = scraper.main(["--site", f"http://127.0.0.1:{self.port}", "--out", tmp, "--delay", "0"])
            self.assertEqual(code, 0)
            with open(os.path.join(tmp, "locations.json"), encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual(len(data["locations"]), 35)

    def test_broken_site_keeps_previous_good_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = os.path.join(tmp, "locations.json")
            with open(good, "w", encoding="utf-8") as fh:
                fh.write('{"keep": "me"}')
            code = scraper.main(["--site", "http://127.0.0.1:1", "--out", tmp, "--delay", "0"])
            self.assertEqual(code, 2)
            with open(good, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), {"keep": "me"})


if __name__ == "__main__":
    unittest.main()

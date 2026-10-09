#!/usr/bin/env python3
"""Step 1 of the pipeline: the scraper.

What it does, in plain words:
  1. Opens the Bellhaven "Our Communities" directory and follows the "Next" links
     until every directory page has been read.
  2. Opens the home page too, because a community can be linked there without
     being in the directory (the directory said 34, the home page said 35).
  3. Opens every community page and reads: name, street, city, state, zip,
     care offerings (plus administrator, phone, and any notice banner).
  4. Saves everything to data/locations.json.

It uses only Python's built-in tools, so nothing needs to be installed.

Safety rule: if the scrape looks broken (too few locations found), the script
stops and does NOT overwrite the last good data/locations.json. Otherwise a
website outage could look like "all communities closed" to the next step.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

DEFAULT_SITE = "https://analyst-assessment-production.up.railway.app"
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(HERE, "data")
USER_AGENT = "bellhaven-sync/1.0 (analyst exercise)"
MIN_EXPECTED_LOCATIONS = 20  # below this we assume the scrape is broken

SLUG_RE = re.compile(r"^/communities/([A-Za-z0-9][A-Za-z0-9_-]*)/?$")
ZIP_LINE_RE = re.compile(r"^(?P<city>.+?),\s*(?P<state>[A-Za-z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)$")
FULL_ADDR_RE = re.compile(
    r"^(?P<street>.+),\s*(?P<city>[^,]+),\s*(?P<state>[A-Za-z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)$"
)


class FetchError(Exception):
    pass


# --------------------------------------------------------------------------
# Downloading
# --------------------------------------------------------------------------
def fetch(url, retries=3, backoff=1.0, timeout=20):
    """Download one page as text. Retries on temporary problems, not on 404."""
    last = None
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=timeout) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="replace")
        except HTTPError as exc:
            last = exc
            if exc.code == 404:  # a missing page will not appear on retry
                break
        except (URLError, TimeoutError, OSError) as exc:
            last = exc
        time.sleep(backoff * (attempt + 1))
    raise FetchError(f"{url}: {last}")


# --------------------------------------------------------------------------
# A tiny HTML reader (built-in html.parser turned into a simple tree)
# --------------------------------------------------------------------------
VOID_TAGS = {"br", "img", "meta", "link", "hr", "input", "area", "base", "col",
             "embed", "source", "track", "wbr"}
BLOCK_TAGS = {"p", "div", "li", "ul", "ol", "dl", "dt", "dd", "h1", "h2", "h3",
              "h4", "section", "header", "footer", "nav", "main", "article", "tr"}


class Node:
    def __init__(self, tag, attrs, parent=None):
        self.tag = tag
        self.attrs = {k: (v or "") for k, v in attrs}
        self.parent = parent
        self.children = []

    def classes(self):
        return self.attrs.get("class", "").split()

    def text(self):
        parts = []
        for child in self.children:
            if isinstance(child, str):
                parts.append(child)
            elif child.tag == "br":
                parts.append("\n")
            elif child.tag in ("script", "style"):
                continue
            elif child.tag in BLOCK_TAGS:
                parts.append("\n" + child.text() + "\n")
            else:
                parts.append(child.text())
        return "".join(parts)

    def walk(self):
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.walk()

    def find_all(self, tag=None, cls=None):
        for node in self.walk():
            if tag and node.tag != tag:
                continue
            if cls and cls not in node.classes():
                continue
            yield node

    def find_first(self, tag=None, cls=None):
        return next(self.find_all(tag, cls), None)


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root", [])
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.cur)
        self.cur.children.append(node)
        if tag not in VOID_TAGS:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, attrs, self.cur))

    def handle_endtag(self, tag):
        node = self.cur
        while node is not self.root and node.tag != tag:
            node = node.parent
        if node is not self.root:
            self.cur = node.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def build_tree(html):
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    return builder.root


def norm(text):
    """Collapse all runs of whitespace into single spaces."""
    return " ".join((text or "").split())


# --------------------------------------------------------------------------
# Reading the pages
# --------------------------------------------------------------------------
def extract_community_links(root):
    """Return [(slug, link text)] for every link to a community page, no repeats."""
    seen, out = set(), []
    for a in root.find_all("a"):
        match = SLUG_RE.match(urlparse(a.attrs.get("href", "")).path)
        if match and match.group(1) not in seen:
            seen.add(match.group(1))
            out.append((match.group(1), norm(a.text())))
    return out


def parse_directory(html):
    """Read one directory page. Returns (links, next_page_href, claimed_total)."""
    root = build_tree(html)
    links = extract_community_links(root)
    next_href = None
    for a in root.find_all("a"):
        href = a.attrs.get("href", "")
        if "page=" in href and "next" in a.text().lower():
            next_href = href
    claimed = re.search(r"(\d+)\s+communities\s+listed", norm(root.text()))
    return links, next_href, int(claimed.group(1)) if claimed else None


def parse_detail(html, url=""):
    """Read one community page into a flat dict. Problems go into 'warnings'."""
    root = build_tree(html)
    warnings = []

    h1 = root.find_first("h1")
    name = norm(h1.text()) if h1 else ""
    if not name:
        warnings.append("no name found")

    fields = {}
    for dl in root.find_all("dl"):
        label = None
        for child in dl.children:
            if not isinstance(child, Node):
                continue
            if child.tag == "dt":
                label = norm(child.text()).lower()
            elif child.tag == "dd" and label:
                fields[label] = child
                label = None

    street = city = state = zip_code = ""
    addr = fields.get("address")
    if addr is None:
        warnings.append("no address block")
    else:
        lines = [norm(x) for x in addr.text().split("\n") if norm(x)]
        if len(lines) >= 2 and ZIP_LINE_RE.match(lines[-1]):
            m = ZIP_LINE_RE.match(lines[-1])
            street = ", ".join(lines[:-1])
            city, state, zip_code = m.group("city"), m.group("state").upper(), m.group("zip")
        elif lines and FULL_ADDR_RE.match(", ".join(lines)):
            m = FULL_ADDR_RE.match(", ".join(lines))
            street, city = m.group("street"), m.group("city")
            state, zip_code = m.group("state").upper(), m.group("zip")
        else:
            warnings.append("address not understood: " + " / ".join(lines))

    care = []
    care_dd = fields.get("care offerings")
    if care_dd is not None:
        care = [norm(b.text()) for b in care_dd.find_all(cls="badge") if norm(b.text())]
        if not care:
            care = [norm(x) for x in re.split(r"[,\n]", care_dd.text()) if norm(x)]
    else:
        warnings.append("no care offerings block")
    care = list(dict.fromkeys(care))  # remove repeats, keep order

    def field_text(label):
        node = fields.get(label)
        return norm(node.text()) if node is not None else ""

    notices = [norm(n.text()) for n in root.find_all(cls="notice") if norm(n.text())]
    known = {"address", "care offerings", "administrator", "phone"}
    extra = {k: norm(v.text()) for k, v in fields.items() if k not in known}

    slug_match = SLUG_RE.match(urlparse(url).path) if url else None
    return {
        "slug": slug_match.group(1) if slug_match else "",
        "url": url,
        "name": name,
        "street": street,
        "city": city,
        "state": state,
        "zip": zip_code,
        "care_offerings": care,
        "administrator": field_text("administrator"),
        "phone": field_text("phone"),
        "notices": notices,
        "extra_fields": extra,
        "warnings": warnings,
    }


def content_hash(loc):
    """A fingerprint of what the website says. If it changes, the page changed."""
    core = {k: loc[k] for k in ("name", "street", "city", "state", "zip", "notices")}
    core["care_offerings"] = sorted(loc["care_offerings"])
    blob = json.dumps(core, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# The whole scrape
# --------------------------------------------------------------------------
def scrape(base_url=DEFAULT_SITE, delay=0.2, fetcher=fetch, log=print):
    base = base_url.rstrip("/")
    snapshot = {}  # every page we saw, kept for debugging
    meta = {
        "base_url": base,
        "directory_pages": 0,
        "directory_claimed_total": None,
        "homepage_claimed_total": None,
        "directory_slugs": 0,
        "homepage_only_slugs": [],
        "fetch_errors": [],
        "warnings": [],
    }
    found = {}  # slug -> list of places we saw it linked from

    # 1) directory pages, following "Next"
    url, visited = base + "/communities", set()
    while url and url not in visited and len(visited) < 50:
        visited.add(url)
        try:
            html = fetcher(url)
        except FetchError as exc:
            meta["fetch_errors"].append(str(exc))
            log(f"  could not read directory page: {url}")
            break
        snapshot[url] = html
        meta["directory_pages"] += 1
        links, next_href, claimed = parse_directory(html)
        if claimed is not None:
            meta["directory_claimed_total"] = claimed
        for slug, _name in links:
            found.setdefault(slug, []).append("directory")
        log(f"  directory page {meta['directory_pages']}: {len(links)} communities")
        url = urljoin(url, next_href) if next_href else None
        time.sleep(delay)
    directory_slugs = set(found)
    meta["directory_slugs"] = len(directory_slugs)

    # 2) the home page (can link to communities the directory does not list)
    try:
        home_html = fetcher(base + "/")
        snapshot[base + "/"] = home_html
        home_root = build_tree(home_html)
        for slug, _name in extract_community_links(home_root):
            found.setdefault(slug, []).append("homepage")
        claimed = re.search(r"(\d+)\s+communities", norm(home_root.text()))
        meta["homepage_claimed_total"] = int(claimed.group(1)) if claimed else None
    except FetchError as exc:
        meta["warnings"].append(f"home page not read: {exc}")

    # 3) about page, saved only so a human can read it later
    try:
        snapshot[base + "/about"] = fetcher(base + "/about")
    except FetchError as exc:
        meta["warnings"].append(f"about page not read: {exc}")

    meta["homepage_only_slugs"] = sorted(set(found) - directory_slugs)

    # 4) every community page
    locations = []
    for slug in found:
        page_url = f"{base}/communities/{slug}"
        try:
            html = fetcher(page_url)
        except FetchError as exc:
            meta["fetch_errors"].append(str(exc))
            log(f"  could not read: {page_url}")
            continue
        snapshot[page_url] = html
        loc = parse_detail(html, page_url)
        loc["slug"] = slug
        loc["found_on"] = sorted(set(found[slug]))
        loc["content_hash"] = content_hash(loc)
        locations.append(loc)
        time.sleep(delay)

    if (meta["directory_claimed_total"] is not None
            and meta["directory_claimed_total"] != meta["directory_slugs"]):
        meta["warnings"].append(
            f"directory says {meta['directory_claimed_total']} communities but "
            f"{meta['directory_slugs']} were found on its pages")
    if (meta["homepage_claimed_total"] is not None
            and meta["homepage_claimed_total"] != len(locations)):
        meta["warnings"].append(
            f"home page says {meta['homepage_claimed_total']} communities but "
            f"{len(locations)} were scraped")
    for loc in locations:
        for w in loc["warnings"]:
            meta["warnings"].append(f"{loc['slug']}: {w}")

    result = {
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": base,
        "meta": meta,
        "locations": locations,
    }
    return result, snapshot


# --------------------------------------------------------------------------
# Saving and running
# --------------------------------------------------------------------------
def write_json_atomic(path, obj):
    """Write to a temp file first, then swap it in, so a crash never leaves half a file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Scrape Bellhaven communities.")
    ap.add_argument("--site", default=os.environ.get("BH_SITE", DEFAULT_SITE))
    ap.add_argument("--out", default=os.environ.get("BH_DATA_DIR", DEFAULT_OUT))
    ap.add_argument("--delay", type=float, default=0.2, help="seconds between requests")
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    print(f"Scraping {args.site} ...")
    result, snapshot = scrape(args.site, delay=args.delay)
    meta, locs = result["meta"], result["locations"]

    write_json_atomic(os.path.join(args.out, "site_snapshot.json"), snapshot)

    print()
    print(f"Found {len(locs)} communities "
          f"(directory pages: {meta['directory_pages']}, "
          f"directory says {meta['directory_claimed_total']}, "
          f"home page says {meta['homepage_claimed_total']})")
    if meta["homepage_only_slugs"]:
        print("Linked from the home page but NOT in the directory: "
              + ", ".join(meta["homepage_only_slugs"]))
    for w in meta["warnings"]:
        print("  warning:", w)
    for e in meta["fetch_errors"]:
        print("  ERROR:", e)

    if len(locs) < MIN_EXPECTED_LOCATIONS or meta["directory_pages"] == 0:
        print(f"\nToo few communities ({len(locs)}). The scrape looks broken, so the "
              "previous data/locations.json was left untouched.")
        return 2

    write_json_atomic(os.path.join(args.out, "locations.json"), result)
    print()
    for loc in locs:
        flag = "  [NOTICE]" if loc["notices"] else ""
        print(f"  {loc['name'][:46]:<46} {loc['city']}, {loc['state']}  "
              f"{'/'.join(loc['care_offerings'])}{flag}")
    print(f"\nSaved {len(locs)} communities to {os.path.join(args.out, 'locations.json')}")
    if meta["fetch_errors"]:
        print("Some pages failed to download. Run the scraper again before trusting the result.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

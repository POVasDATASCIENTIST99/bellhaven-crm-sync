"""Step 3: the review app. A small local web page where a person approves or rejects
each proposed change. Nothing is written to the CRM until you click Approve.

Run it with:  python sync.py serve        then open http://127.0.0.1:8765
It only listens on your own computer (127.0.0.1), and every button carries a
one-time token so another web page cannot click them for you.
"""
import html
import secrets
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

import applier

SECTIONS = [
    ("chow", "Change of ownership (billing rule)",
     "These accounts have revenue history AND an unpaid balance, so their parent must NOT change. "
     "The old account stays as it is and points to the account that now represents the community."),
    ("duplicate", "Duplicates",
     "Two or more CRM accounts describe the same place. The best one survives; the others are marked "
     "Inactive and point to it (there is no merge or delete in this API)."),
    ("update", "Fixes to existing accounts",
     "The account is the right place, but its name, address or parent is out of date."),
    ("create", "New accounts to create",
     "The website lists a community that has no CRM account at all."),
    ("missing", "Under Bellhaven but not on the website",
     "Absence from a website is not proof of closure, so these are flagged for a person to check."),
    ("review", "Possible matches that need a person",
     "Looks similar but the evidence is not strong enough to link automatically."),
]

CSS = """
body{font-family:Segoe UI,Arial,sans-serif;margin:0;background:#f4f5f2;color:#222}
header{background:#2e5d50;color:#fff;padding:14px 24px}
header h1{margin:0;font-size:20px} header p{margin:4px 0 0;font-size:13px;opacity:.9}
main{max-width:1000px;margin:0 auto;padding:18px 24px 80px}
.bar{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0}
.pill{background:#fff;border:1px solid #d8dbd2;border-radius:16px;padding:4px 12px;font-size:13px}
h2{margin:30px 0 4px;color:#2e5d50} .desc{margin:0 0 10px;color:#555;font-size:14px}
.card{background:#fff;border:1px solid #d8dbd2;border-radius:6px;padding:14px 16px;margin:10px 0}
.card h3{margin:0 0 6px;font-size:16px}
.badge{font-size:11px;border-radius:3px;padding:2px 7px;margin-left:6px;color:#fff}
.high{background:#2e7d32}.medium{background:#b26a00}.low{background:#b23b3b}
.st-pending{background:#666}.st-applied{background:#2e7d32}.st-rejected{background:#888}
.st-failed{background:#b23b3b}.st-stale{background:#999}.st-approved{background:#1565c0}
ul{margin:6px 0 6px 18px;padding:0;font-size:14px} table{border-collapse:collapse;margin:8px 0;font-size:13px;width:100%}
th,td{border:1px solid #e1e3dc;padding:4px 8px;text-align:left;vertical-align:top} th{background:#f0f2ec}
.warn{background:#fff4e0;border-left:4px solid #e0a030;padding:6px 10px;font-size:13px;margin:6px 0}
.err{background:#fde8e8;border-left:4px solid #c33;padding:6px 10px;font-size:13px;margin:6px 0}
.ok{background:#e8f5e9;border-left:4px solid #2e7d32;padding:6px 10px;font-size:13px;margin:6px 0}
button{font-size:14px;padding:6px 14px;border:0;border-radius:4px;cursor:pointer;margin-right:8px}
.approve{background:#2e7d32;color:#fff}.reject{background:#8a8a8a;color:#fff}.neutral{background:#1565c0;color:#fff}
form{display:inline} .small{font-size:12px;color:#666} code{background:#eef0ea;padding:1px 4px;border-radius:3px}
details{margin-top:8px;font-size:13px}
"""


def e(text):
    return html.escape("" if text is None else str(text))


def render_card(key, entry, token):
    p, status = entry["proposal"], entry["status"]
    parts = [f'<div class="card" id="p-{e(key)}"><h3>{e(p["title"])}'
             f'<span class="badge {e(p["confidence"])}">{e(p["confidence"])} confidence</span>'
             f'<span class="badge st-{e(status)}">{e(status)}</span></h3>']
    if p.get("sop"):
        sop = p["sop"]
        parts.append(f'<div class="small">Billing check: revenue {e(sop["revenue"])}, '
                     f'outstanding AR {e(sop["ar"])} &rarr; <b>{"preserve old account (CHOW)" if sop["path"] == "chow" else "re-parent directly"}</b></div>')
    parts.append("<ul>" + "".join(f"<li>{e(x)}</li>" for x in p["evidence"]) + "</ul>")
    for w in p.get("warnings", []):
        parts.append(f'<div class="warn">{e(w)}</div>')
    shown = [c for c in p["changes"] if c["field"] != "note"]
    notes = [c for c in p["changes"] if c["field"] == "note"]
    if shown:
        rows = "".join(f'<tr><td>{e(c["account_name"])}<br><span class="small">{e(c["account_id"])}</span></td>'
                       f'<td>{e(c["field"])}</td><td>{e(c["old"]) or "<i>(empty)</i>"}</td><td><b>{e(c["new"])}</b></td></tr>'
                       for c in shown)
        parts.append(f"<table><tr><th>Account</th><th>Field</th><th>Now</th><th>Proposed</th></tr>{rows}</table>")
    if notes:
        parts.append(f'<details><summary>Note that will be saved on the account</summary>{e(notes[0]["new"])}</details>')
    if entry.get("error"):
        parts.append(f'<div class="err"><b>Not written:</b> {e(entry["error"])}</div>')
    created = entry.get("progress", {}).get("created", {})
    if status == "applied":
        extra = f" New account id: <code>{e(', '.join(created.values()))}</code>." if created else ""
        parts.append(f'<div class="ok">Written to the CRM and verified.{extra}</div>')
    form_head = f'<input type="hidden" name="token" value="{e(token)}"><input type="hidden" name="key" value="{e(key)}">'
    if status in ("pending", "failed"):
        label = "Retry" if status == "failed" else "Approve and write to CRM"
        parts.append(f'<form method="post" action="/decide">{form_head}<input type="hidden" name="decision" value="approve">'
                     f'<button class="approve">{label}</button></form>'
                     f'<form method="post" action="/decide">{form_head}<input type="hidden" name="decision" value="reject">'
                     f'<button class="reject">Reject</button></form>')
    elif status == "rejected":
        parts.append(f'<form method="post" action="/decide">{form_head}<input type="hidden" name="decision" value="reset">'
                     f'<button class="neutral">Undo reject</button></form>')
    hist = "".join(f"<li>{e(h['at'])}: {e(h['event'])}</li>" for h in entry["history"])
    parts.append(f'<details><summary>History</summary><ul>{hist}</ul></details></div>')
    return "".join(parts)


def render_page(ledger, token, message=""):
    counts = ledger.counts()
    out = [f"<!doctype html><html><head><meta charset='utf-8'><title>Bellhaven review</title><style>{CSS}</style></head><body>",
           "<header><h1>Bellhaven CRM review</h1><p>Each card is a proposed change with its evidence. "
           "Nothing is written until you click Approve.</p></header><main>"]
    if message:
        out.append(f'<div class="ok">{e(message)}</div>')
    out.append('<div class="bar">' + "".join(
        f'<span class="pill">{n}: <b>{counts[n]}</b></span>' for n in ("pending", "applied", "rejected", "failed", "stale")) +
        f'<form method="post" action="/refresh"><input type="hidden" name="token" value="{e(token)}">'
        '<button class="neutral">Refresh proposals from CRM</button></form></div>')
    for ptype, title, desc in SECTIONS:
        items = [(k, v) for k, v in ledger.entries.items() if v["proposal"]["type"] == ptype and v["status"] != "stale"]
        if not items:
            continue
        pending_high = sum(1 for _, v in items if v["status"] == "pending" and v["proposal"]["confidence"] == "high")
        out.append(f"<h2>{e(title)} ({len(items)})</h2><p class='desc'>{e(desc)}</p>")
        if pending_high:
            out.append(f'<form method="post" action="/approve-group" onsubmit="return confirm(\'Approve and write all {pending_high} '
                       f'high-confidence items in this section? Read them first.\')">'
                       f'<input type="hidden" name="token" value="{e(token)}"><input type="hidden" name="type" value="{e(ptype)}">'
                       f'<button class="neutral">Approve all {pending_high} high-confidence items in this section</button></form>')
        order = {"pending": 0, "failed": 0, "approved": 1, "applied": 2, "rejected": 3}
        for key, entry in sorted(items, key=lambda kv: (order.get(kv[1]["status"], 9), kv[1]["proposal"]["title"])):
            out.append(render_card(key, entry, token))
    out.append("</main></body></html>")
    return "".join(out)


class ReviewApp:
    """Holds what the web page needs. The CRM is created lazily so the page can open even if the token is missing."""

    def __init__(self, ledger, crm_factory, refresh_fn=None, host="127.0.0.1", port=8765):
        self.ledger, self.crm_factory, self.refresh_fn = ledger, crm_factory, refresh_fn
        self.host, self.port = host, port
        self.token = secrets.token_urlsafe(16)
        self.lock = threading.Lock()
        self.message = ""
        self._crm = None

    def crm(self):
        if self._crm is None:
            self._crm = self.crm_factory()
        return self._crm

    def handle_post(self, path, form):
        if form.get("token", [""])[0] != self.token:
            return 403, "Bad token. Reload the page."
        with self.lock:
            try:
                if path == "/decide":
                    key, decision = form["key"][0], form["decision"][0]
                    if key not in self.ledger.entries:
                        return 404, "Unknown proposal"
                    if decision == "approve":
                        ok = applier.approve_and_apply(self.crm(), self.ledger, key)
                        self.message = "Approved and written to the CRM." if ok else "Approved, but the write failed. See the red message."
                    elif decision == "reject":
                        applier.reject(self.ledger, key)
                        self.message = "Rejected. Nothing was written."
                    elif decision == "reset":
                        applier.reset(self.ledger, key)
                        self.message = "Back to pending."
                    else:
                        return 400, "Unknown decision"
                elif path == "/approve-group":
                    ptype, done, failed = form["type"][0], 0, 0
                    for key, entry in list(self.ledger.entries.items()):
                        p = entry["proposal"]
                        if entry["status"] == "pending" and p["type"] == ptype and p["confidence"] == "high":
                            if applier.approve_and_apply(self.crm(), self.ledger, key):
                                done += 1
                            else:
                                failed += 1
                    self.message = f"Approved {done} item(s)" + (f"; {failed} failed, see the red messages." if failed else ".")
                elif path == "/refresh":
                    if not self.refresh_fn:
                        return 400, "Refresh is not available in this mode"
                    summary = self.refresh_fn()
                    self.message = f"Refreshed: {summary}"
                else:
                    return 404, "Not found"
            except Exception as exc:  # show the problem on the page instead of crashing the server
                self.message = f"Error: {exc}"
        return 303, "/"

    def make_handler(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, body, ctype="text/html; charset=utf-8", location=None):
                data = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                if location:
                    self.send_header("Location", location)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.split("?")[0] != "/":
                    return self._send(404, "Not found", "text/plain")
                with app.lock:
                    page = render_page(app.ledger, app.token, app.message)
                    app.message = ""
                self._send(200, page)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                form = parse_qs(self.rfile.read(length).decode("utf-8"))
                code, body = app.handle_post(self.path, form)
                if code == 303:
                    return self._send(303, "", location=body)
                self._send(code, body, "text/plain")

        return Handler

    def server(self):
        return HTTPServer((self.host, self.port), self.make_handler())

#!/usr/bin/env python3
"""The one command that runs the pipeline.

    python sync.py run        scrape the website, read the CRM, make proposals (writes NOTHING to the CRM)
    python sync.py serve      open the review app at http://127.0.0.1:8765
    python sync.py apply      write any approved-but-not-yet-written proposals
    python sync.py status     show counts
    python sync.py check-api  one safe test that the CRM token works and that PATCH is accepted (changes nothing)

Handy options:
    run   --skip-scrape            reuse data/locations.json instead of visiting the website
    run   --accounts-file FILE     read CRM accounts from a saved file instead of the API
    serve --offline                rehearse against an in-memory copy of the CRM (nothing real is touched)

The CRM token comes from the BH_TOKEN environment variable and is never saved.
"""
import argparse
import json
import os
import re
import sys

import applier
import scraper
from crm import CRMClient, CRMError
from fake_crm import FakeCRM
from matcher import build_proposals
from review_app import ReviewApp
from store import Ledger

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("BH_DATA_DIR", os.path.join(HERE, "data"))
LOCATIONS = os.path.join(DATA, "locations.json")
SNAPSHOT = os.path.join(DATA, "site_snapshot.json")
LEDGER = os.path.join(DATA, "decisions.json")


def load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def about_text():
    """The plain text of the website's About page, used as supporting evidence."""
    try:
        snap = load_json(SNAPSHOT)
    except OSError:
        return ""
    for url, page in snap.items():
        if url.rstrip("/").endswith("/about"):
            page = re.sub(r"<(style|script).*?</\1>", " ", page, flags=re.S)
            return " ".join(re.sub(r"<[^>]+>", " ", page).split())
    return ""


def load_accounts(args):
    if getattr(args, "accounts_file", None):
        data = load_json(args.accounts_file)
        return data["data"] if isinstance(data, dict) else data
    return CRMClient().list_accounts()


def make_proposals(args, accounts=None):
    scraped = load_json(LOCATIONS)
    accounts = accounts if accounts is not None else load_accounts(args)
    meta = scraped["meta"]
    allow_missing = not meta["fetch_errors"]
    if not allow_missing:
        print("Some website pages failed to download, so 'not on website' checks are skipped this run.")
    return build_proposals(scraped["locations"], accounts, about_text=about_text(), allow_missing=allow_missing)


def cmd_run(args):
    os.makedirs(DATA, exist_ok=True)
    if not args.skip_scrape:
        code = scraper.main(["--out", DATA])
        if code == 2:
            print("Stopping: the scrape looks broken. Nothing was changed.")
            return 2
    proposals, stats = make_proposals(args)
    ledger = Ledger(LEDGER)
    summary = ledger.merge(proposals)
    print()
    print(f"{stats['locations']} website locations checked; {stats['confident_matches']} already correct.")
    print(f"{stats['proposals']} proposals this run: {summary['new']} new, {summary['still_pending']} still pending, "
          f"{summary['already_decided']} already decided (not shown again), {summary['reappeared']} reappeared, "
          f"{summary['stale']} no longer needed.")
    print("Counts now:", ledger.counts())
    print("Nothing was written to the CRM. Next:  python sync.py serve")
    return 0


def cmd_serve(args):
    ledger = Ledger(LEDGER)
    if args.offline:
        accounts = load_json(args.accounts_file)["data"]
        offline = FakeCRM(accounts)
        crm_factory = lambda: offline  # noqa: E731
        print("OFFLINE REHEARSAL: nothing you click will touch the real CRM.")
    else:
        crm_factory = CRMClient

    def refresh():
        acc = (offline.list_accounts() if args.offline else CRMClient().list_accounts())
        proposals, _ = make_proposals(args, acc)
        return ledger.merge(proposals)

    app = ReviewApp(ledger, crm_factory, refresh, port=args.port)
    server = app.server()
    print(f"Review app running at http://127.0.0.1:{args.port}  (press Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


def cmd_apply(args):
    ledger = Ledger(LEDGER)
    print(applier.apply_all_approved(CRMClient(), ledger))
    return 0


def cmd_check_api(args):
    """Read your own account record, then 'change' one field to the value it already has.
    Proves the token works and that the PATCH request format is accepted, without changing any data."""
    crm = CRMClient()
    print("Token accepted:", crm.me())
    accounts = crm.list_accounts()
    print(f"Read {len(accounts)} accounts.")
    probe = accounts[0]
    crm.patch_account(probe["account_id"], {"status": probe["status"]})
    after = crm.get_account(probe["account_id"])
    ok = after["status"] == probe["status"]
    print("PATCH accepted and nothing changed." if ok else "PATCH changed something unexpected!", probe["account_id"])
    return 0 if ok else 1


def cmd_status(args):
    ledger = Ledger(LEDGER)
    print(ledger.counts())
    for key, entry in ledger.entries.items():
        if entry["status"] in ("failed",):
            print(" FAILED", key, entry["proposal"]["title"], "->", entry["error"])
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--skip-scrape", action="store_true")
    r.add_argument("--accounts-file")
    r.set_defaults(fn=cmd_run)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--offline", action="store_true")
    s.add_argument("--accounts-file", default=os.path.join(HERE, "tests", "fixtures", "accounts.json"))
    s.set_defaults(fn=cmd_serve)
    sub.add_parser("apply").set_defaults(fn=cmd_apply)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("check-api").set_defaults(fn=cmd_check_api)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except CRMError as exc:
        print("CRM problem:", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())

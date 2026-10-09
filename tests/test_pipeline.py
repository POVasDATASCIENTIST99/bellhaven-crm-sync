"""Tests for the ledger (store.py), the write-back (applier.py) and the review app."""
import json
import os
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen, build_opener, HTTPRedirectHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import applier  # noqa: E402
import matcher  # noqa: E402
import store  # noqa: E402
from crm import CRMError  # noqa: E402
from fake_crm import FakeCRM  # noqa: E402
from review_app import ReviewApp  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return json.load(fh)


LOCS = load("locations.json")["locations"]
ACCTS = load("accounts.json")["data"]
ABOUT = open(os.path.join(FIX, "about.txt"), encoding="utf-8").read()


def propose(accounts):
    return matcher.build_proposals(LOCS, accounts, about_text=ABOUT, today="2026-10-09")[0]


def key_for(props, needle):
    return next(p["key"] for p in props if needle in p["title"])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.crm = FakeCRM(ACCTS)
        self.ledger = store.Ledger(os.path.join(self.tmp.name, "decisions.json"))
        self.props = propose(ACCTS)
        self.ledger.merge(self.props)

    def tearDown(self):
        self.tmp.cleanup()


class LedgerTests(Base):
    def test_nothing_is_written_until_approved(self):
        self.assertEqual(self.crm.log, [])
        self.assertEqual(self.ledger.counts()["pending"], 29)

    def test_rerun_does_not_repropose_decided_items(self):
        a, b = key_for(self.props, "Bellhaven of Portsmouth"), key_for(self.props, "Union Square")
        self.assertTrue(applier.approve_and_apply(self.crm, self.ledger, a))
        applier.reject(self.ledger, b)
        # run the whole pipeline again against the CRM as it is now
        props2 = propose(self.crm.list_accounts())
        summary = self.ledger.merge(props2)
        self.assertEqual(summary["new"], 0)
        self.assertEqual(summary["already_decided"], 1)       # the rejected one, still rejected
        self.assertEqual(self.ledger.entries[b]["status"], "rejected")
        self.assertEqual(self.ledger.entries[a]["status"], "applied")
        self.assertNotIn(a, [p["key"] for p in props2])
        self.assertEqual(self.ledger.counts()["pending"], 27)  # 29 - applied - rejected, no duplicates added

    def test_running_twice_with_no_decisions_adds_nothing(self):
        summary = self.ledger.merge(propose(ACCTS))
        self.assertEqual((summary["new"], summary["still_pending"]), (0, 29))
        self.assertEqual(len(self.ledger.entries), 29)

    def test_pending_item_becomes_stale_if_facts_change(self):
        key = key_for(self.props, "Bellhaven of Portsmouth")
        self.crm.patch_account("001CF3LDWVRGL09P4F", {"billing_zip": "45662"})   # someone fixed it by hand
        self.ledger.merge(propose(self.crm.list_accounts()))
        self.assertEqual(self.ledger.entries[key]["status"], "stale")
        with self.assertRaises(applier.ApplyError):
            applier.apply_entry(self.crm, self.ledger, key)

    def test_applied_item_that_reappears_returns_to_pending(self):
        key = key_for(self.props, "Bellhaven of Portsmouth")
        applier.approve_and_apply(self.crm, self.ledger, key)
        self.crm.patch_account("001CF3LDWVRGL09P4F", {"billing_zip": "45626"})   # someone undid it
        self.ledger.merge(propose(self.crm.list_accounts()))
        self.assertEqual(self.ledger.entries[key]["status"], "pending")

    def test_ledger_survives_a_restart(self):
        key = key_for(self.props, "Union Square")
        applier.reject(self.ledger, key)
        reloaded = store.Ledger(self.ledger.path)
        self.assertEqual(reloaded.entries[key]["status"], "rejected")


class ApplyTests(Base):
    def test_simple_update_is_written_and_verified(self):
        key = key_for(self.props, "Bellhaven of Portsmouth")
        self.assertTrue(applier.approve_and_apply(self.crm, self.ledger, key))
        self.assertEqual(self.crm.accounts["001CF3LDWVRGL09P4F"]["billing_zip"], "45662")
        self.assertEqual(self.crm.log, [("PATCH", "001CF3LDWVRGL09P4F", {"billing_zip": "45662"})])

    def test_chow_creates_new_account_and_points_old_one_without_touching_it(self):
        key = key_for(self.props, "Bellhaven of Tiffin")
        before = dict(self.crm.accounts["001U6RW32TY0WSXZZB"])
        self.assertTrue(applier.approve_and_apply(self.crm, self.ledger, key))
        new_id = self.ledger.entries[key]["progress"]["created"]["new1"]
        old, new = self.crm.accounts["001U6RW32TY0WSXZZB"], self.crm.accounts[new_id]
        self.assertEqual(old["chow_current_account"], new_id)
        for field in before:                                    # everything else on the old account is unchanged
            if field != "chow_current_account":
                self.assertEqual(old[field], before[field], field)
        self.assertEqual(new["parent_id"], "0015QAPLGS3FVYEEEM")
        self.assertEqual(new["billing_street"], "45 St Lawrence Dr")

    def test_chow_resumes_after_failure_without_creating_a_second_account(self):
        key = key_for(self.props, "Bellhaven of Tiffin")
        self.crm.fail_patch_for.add("001U6RW32TY0WSXZZB")
        self.assertFalse(applier.approve_and_apply(self.crm, self.ledger, key))
        self.assertEqual(self.ledger.entries[key]["status"], "failed")
        self.assertEqual([x[0] for x in self.crm.log], ["POST"])
        self.crm.fail_patch_for.clear()
        self.assertTrue(applier.approve_and_apply(self.crm, self.ledger, key))
        self.assertEqual([x[0] for x in self.crm.log], ["POST", "PATCH"])   # still only ONE create

    def test_create_is_not_repeated_if_the_account_already_exists(self):
        key = key_for(self.props, "Bellhaven of Batavia")
        # pretend an earlier attempt created it but the program crashed before saving progress
        action = self.ledger.entries[key]["proposal"]["actions"][0]
        existing = self.crm.create_account(action["fields"])
        self.assertTrue(applier.approve_and_apply(self.crm, self.ledger, key))
        self.assertEqual([x[0] for x in self.crm.log], ["POST"])
        self.assertEqual(self.ledger.entries[key]["progress"]["created"]["new1"], existing["account_id"])

    def test_refuses_to_write_if_account_changed_since_proposal(self):
        key = key_for(self.props, "Bellhaven of Portsmouth")
        self.crm.accounts["001CF3LDWVRGL09P4F"]["billing_zip"] = "99999"   # changed by someone else
        self.assertFalse(applier.approve_and_apply(self.crm, self.ledger, key))
        self.assertIn("changed since", self.ledger.entries[key]["error"])
        self.assertEqual(self.crm.log, [])

    def test_failed_item_can_be_retried(self):
        key = key_for(self.props, "Bellhaven of Portsmouth")
        self.crm.fail_patch_for.add("001CF3LDWVRGL09P4F")
        self.assertFalse(applier.approve_and_apply(self.crm, self.ledger, key))
        self.crm.fail_patch_for.clear()
        self.assertTrue(applier.approve_and_apply(self.crm, self.ledger, key))
        self.assertEqual(self.ledger.entries[key]["status"], "applied")

    def test_rejected_item_is_never_written(self):
        key = key_for(self.props, "Bellhaven of Portsmouth")
        applier.reject(self.ledger, key)
        with self.assertRaises(applier.ApplyError):
            applier.apply_entry(self.crm, self.ledger, key)
        self.assertEqual(self.crm.log, [])

    def test_duplicate_marks_loser_inactive_and_points_to_survivor(self):
        key = next(p["key"] for p in self.props if p["type"] == "duplicate" and "Owosso" in p["title"])
        applier.approve_and_apply(self.crm, self.ledger, key)
        loser = self.crm.accounts["001QU150PM4Z15UA71"]
        self.assertEqual((loser["status"], loser["duplicate_of_account"]), ("Inactive", "001EGU7BMJ942ZTRE6"))
        self.assertEqual(self.crm.accounts["001EGU7BMJ942ZTRE6"]["status"], "Active")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class AppTests(Base):
    def setUp(self):
        super().setUp()
        self.app = ReviewApp(self.ledger, lambda: self.crm, port=0)
        self.server = self.app.server()
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        super().tearDown()

    def post(self, path, data):
        req = Request(self.base + path, data=urlencode(data).encode(), method="POST")
        try:
            return build_opener(NoRedirect).open(req)
        except HTTPError as exc:
            return exc

    def test_page_shows_proposals_and_billing_rule(self):
        page = urlopen(self.base + "/").read().decode()
        self.assertIn("Bellhaven CRM review", page)
        self.assertIn("preserve old account (CHOW)", page)
        self.assertIn("Bellhaven of Tiffin", page)

    def test_post_without_token_is_refused(self):
        key = key_for(self.props, "Bellhaven of Portsmouth")
        resp = self.post("/decide", {"key": key, "decision": "approve", "token": "wrong"})
        self.assertEqual(resp.code, 403)
        self.assertEqual(self.crm.log, [])

    def test_approve_button_writes_and_page_shows_applied(self):
        key = key_for(self.props, "Bellhaven of Portsmouth")
        resp = self.post("/decide", {"key": key, "decision": "approve", "token": self.app.token})
        self.assertEqual(resp.code, 303)
        self.assertEqual(self.crm.accounts["001CF3LDWVRGL09P4F"]["billing_zip"], "45662")
        self.assertIn("Written to the CRM and verified", urlopen(self.base + "/").read().decode())

    def test_reject_button_writes_nothing(self):
        key = key_for(self.props, "Bellhaven of Portsmouth")
        self.post("/decide", {"key": key, "decision": "reject", "token": self.app.token})
        self.assertEqual(self.crm.log, [])
        self.assertEqual(self.ledger.entries[key]["status"], "rejected")

    def test_approve_group_only_touches_high_confidence_items_of_that_type(self):
        self.post("/approve-group", {"type": "create", "token": self.app.token})
        self.assertEqual(len([x for x in self.crm.log if x[0] == "POST"]), 3)
        self.assertEqual(len(self.crm.log), 3)

    def test_html_is_escaped(self):
        key = key_for(self.props, "Bellhaven of Portsmouth")
        self.ledger.entries[key]["proposal"]["title"] = "<script>alert(1)</script>"
        page = urlopen(self.base + "/").read().decode()
        self.assertNotIn("<script>alert(1)</script>", page)


if __name__ == "__main__":
    unittest.main()

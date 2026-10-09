"""Tests for matcher.py using the REAL data (121 CRM accounts, 35 website locations)."""
import collections
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import matcher  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return json.load(fh)


LOCS = load("locations.json")["locations"]
ACCTS = load("accounts.json")["data"]
ABOUT = open(os.path.join(FIX, "about.txt"), encoding="utf-8").read()
BH = "0015QAPLGS3FVYEEEM"


def by_title(props, needle):
    hits = [p for p in props if needle in p["title"]]
    assert len(hits) == 1, (needle, [p["title"] for p in hits])
    return hits[0]


class HelperTests(unittest.TestCase):
    def test_street_cleaning(self):
        self.assertEqual(matcher.norm_street("1125 Logan Boulevard"), matcher.norm_street("1125 Logan Blvd"))
        self.assertEqual(matcher.norm_street("1250 Northwest Franklin St"), matcher.norm_street("1250 NW Franklin Street"))
        self.assertEqual(matcher.norm_street("3313 Wilmington Pk"), matcher.norm_street("3313 Wilmington Pike"))
        self.assertNotEqual(matcher.norm_street("1120 W Main St"), matcher.norm_street("1120 E Main St"))

    def test_po_box(self):
        self.assertTrue(matcher.is_po_box("PO Box 517"))
        self.assertFalse(matcher.is_po_box("517 Pobox Road"))

    def test_billing_rule_needs_both(self):
        self.assertTrue(matcher.has_billing({"lifetime_revenue": 10, "outstanding_ar": 1}))
        self.assertFalse(matcher.has_billing({"lifetime_revenue": 10, "outstanding_ar": 0}))
        self.assertFalse(matcher.has_billing({"lifetime_revenue": 0, "outstanding_ar": 5}))

    def test_name_decoys_do_not_match(self):
        # "Bellhaven of Carlisle" (PA) must not match "Bellhaven of New Carlisle" (OH)
        loc = next(l for l in LOCS if l["slug"] == "bellhaven-of-carlisle")
        acct = next(a for a in ACCTS if a["name"] == "Bellhaven of New Carlisle")
        self.assertEqual(matcher.same_place(loc, acct)[0], None)
        # "Amberly Manor" in Hudson OH must not match "Amberly Manor" in Colorado Springs
        loc = next(l for l in LOCS if l["slug"] == "amberly-manor")
        acct = next(a for a in ACCTS if a["name"] == "Amberly Manor")
        self.assertEqual(matcher.same_place(loc, acct)[0], None)


class RealDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.props, cls.stats = matcher.build_proposals(LOCS, ACCTS, about_text=ABOUT, today="2026-10-09")

    def test_totals(self):
        self.assertEqual(self.stats["locations"], 35)
        self.assertEqual(len(self.props), 29)
        self.assertEqual(self.stats["confident_matches"], 16)
        self.assertEqual(dict(collections.Counter(p["type"] for p in self.props)),
                         {"update": 13, "duplicate": 7, "chow": 3, "create": 3, "missing": 2, "review": 1})

    def test_keys_are_unique(self):
        keys = [p["key"] for p in self.props]
        self.assertEqual(len(keys), len(set(keys)))

    def test_billing_sop_tiffin_and_marietta_are_chow_not_reparent(self):
        for name in ("Bellhaven of Tiffin", "Bellhaven of Marietta"):
            p = by_title(self.props, name)
            self.assertEqual(p["type"], "chow")
            self.assertEqual(p["sop"]["path"], "chow")
            ops = [a["op"] for a in p["actions"]]
            self.assertEqual(ops, ["create", "patch"])
            patch = p["actions"][1]
            self.assertEqual(set(patch["fields"]), {"chow_current_account"})   # nothing else on the old account
            self.assertEqual(patch["fields"]["chow_current_account"], "$ref:new1")
            self.assertEqual(p["actions"][0]["fields"]["parent_id"], BH)       # new account is under Bellhaven

    def test_no_proposal_ever_changes_parent_of_an_account_with_billing(self):
        billing = {a["account_id"] for a in ACCTS if matcher.has_billing(a)}
        for p in self.props:
            for act in p["actions"]:
                if act["op"] == "patch" and act["account_id"] in billing:
                    self.assertNotIn("parent_id", act["fields"], p["title"])

    def test_direct_reparent_when_no_ar(self):
        for name in ("Bellhaven Crossings of Lima", "Bellhaven Meadows of Findlay"):
            p = by_title(self.props, name)
            self.assertEqual(p["type"], "update")
            self.assertEqual(p["sop"]["path"], "direct")
            self.assertEqual(p["actions"][0]["fields"], {"parent_id": BH})

    def test_lima_evidence_quotes_about_page(self):
        p = by_title(self.props, "Bellhaven Crossings of Lima")
        self.assertTrue(any("Harborview" in line and "About page" in line for line in p["evidence"]))

    def test_sandusky_points_to_existing_millstone_account_without_creating(self):
        p = by_title(self.props, "Bellhaven of Sandusky")
        self.assertEqual(p["type"], "chow")
        self.assertEqual([a["op"] for a in p["actions"]], ["patch"])
        self.assertEqual(p["actions"][0]["fields"], {"chow_current_account": "0017JP8Z1UQ763BVK3"})

    def test_owosso_duplicate_keeps_the_one_whose_phone_matches(self):
        p = [x for x in self.props if x["type"] == "duplicate" and "Owosso" in x["title"]][0]
        patch = p["actions"][0]
        self.assertEqual(patch["account_id"], "001QU150PM4Z15UA71")
        self.assertEqual(patch["fields"]["duplicate_of_account"], "001EGU7BMJ942ZTRE6")
        self.assertEqual(patch["fields"]["status"], "Inactive")

    def test_triple_duplicates_at_monroe_all_point_to_the_bellhaven_account(self):
        monroe = [x for x in self.props if x["type"] == "duplicate" and "Monroe" in x["title"]]
        self.assertEqual(len(monroe), 2)
        for p in monroe:
            self.assertEqual(p["actions"][0]["fields"]["duplicate_of_account"], "001U1750VLVJAGG1S5")

    def test_po_box_and_zip_typo_are_fixed_not_recreated(self):
        ash = by_title(self.props, "Bellhaven of Ashtabula")
        self.assertEqual(ash["actions"][0]["fields"], {"billing_street": "3156 W Prospect Rd"})
        por = by_title(self.props, "Bellhaven of Portsmouth")
        self.assertEqual(por["actions"][0]["fields"], {"billing_zip": "45662"})
        self.assertFalse([p for p in self.props if p["type"] == "create" and ("Ashtabula" in p["title"] or "Portsmouth" in p["title"])])

    def test_rebrands_are_renames(self):
        for old, new in (("Riverbend Manor Care Center", "Bellhaven of Chagrin Falls"),
                         ("Sunny Acres Retirement Home", "Bellhaven Willow Creek"),
                         ("Chesterton Senior Commons", "Bellhaven of Chesterton")):
            p = by_title(self.props, old)
            self.assertEqual(p["actions"][0]["fields"], {"name": new})

    def test_new_accounts_only_for_three_places(self):
        created = sorted(p["location"]["name"] for p in self.props if p["type"] == "create")
        self.assertEqual(created, ["Amberly Manor", "Bellhaven of Batavia", "Bellhaven of Carlisle"])

    def test_union_square_is_only_a_low_confidence_review(self):
        p = by_title(self.props, "Union Square")
        self.assertEqual((p["type"], p["confidence"]), ("review", "low"))
        self.assertFalse([x for x in self.props if x["type"] == "create" and "Union Square" in x["title"]])

    def test_missing_accounts_are_flagged_not_deactivated(self):
        missing = [p for p in self.props if p["type"] == "missing"]
        self.assertEqual(sorted(p["title"] for p in missing),
                         ["Not on website: 'Bellhaven Care Center of Alliance'", "Not on website: 'Bellhaven of Coldwater'"])
        for p in missing:
            self.assertEqual(p["actions"][0]["fields"]["status"], "Needs Review")
            self.assertNotIn("parent_id", p["actions"][0]["fields"])

    def test_same_inputs_same_keys(self):
        again, _ = matcher.build_proposals(LOCS, ACCTS, about_text=ABOUT, today="2030-01-01")
        self.assertEqual(sorted(p["key"] for p in again), sorted(p["key"] for p in self.props))

    def test_skipping_missing_check_when_website_incomplete(self):
        props, _ = matcher.build_proposals(LOCS, ACCTS, about_text=ABOUT, allow_missing=False)
        self.assertFalse([p for p in props if p["type"] == "missing"])
        self.assertFalse([p for p in props if "Sandusky" in p["title"]])


class AfterFixTests(unittest.TestCase):
    def test_nothing_left_to_propose_once_everything_is_fixed(self):
        """Simulate every proposal being applied, then re-run: the matcher should have nothing to say."""
        from fake_crm import FakeCRM
        import applier, store, tempfile
        crm = FakeCRM(ACCTS)
        props, _ = matcher.build_proposals(LOCS, ACCTS, about_text=ABOUT, today="2026-10-09")
        with tempfile.TemporaryDirectory() as tmp:
            ledger = store.Ledger(os.path.join(tmp, "d.json"))
            ledger.merge(props)
            for key in list(ledger.entries):
                self.assertTrue(applier.approve_and_apply(crm, ledger, key), ledger.entries[key]["error"])
            props2, stats2 = matcher.build_proposals(LOCS, crm.list_accounts(), about_text=ABOUT, today="2026-10-10")
            self.assertEqual([p["title"] for p in props2], [])


if __name__ == "__main__":
    unittest.main()

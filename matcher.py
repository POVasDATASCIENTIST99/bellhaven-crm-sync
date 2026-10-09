"""Step 2 of the pipeline: the matcher.

Input : the scraped website locations + the CRM accounts.
Output: a list of PROPOSALS. A proposal is a suggested change with its evidence.
        The matcher never writes anything. A human approves each proposal later.

Everything here is a pure function: same inputs always give the same proposals.
That is what makes re-runs safe (see store.py for the "already decided" part).

The rules, in plain words
-------------------------
1. A website location and a CRM account are the SAME place if their street
   address matches after cleaning (Boulevard = Blvd, Northwest = NW, ...) and the
   zip matches. Weaker versions of the same idea are also strong:
     - same street + same city, zip is different (a typo in one system), or
     - same name + same city, street is different (e.g. the CRM has a PO Box).
2. Names are NOT used alone, because the CRM names are often stale or rebranded.
3. If two or more accounts are the same place, one survives and the others are
   duplicates (status Inactive + duplicate_of_account).
4. If the surviving account is not under the Bellhaven parent, it must move.
   BILLING SOP: if lifetime_revenue > 0 AND outstanding_ar > 0 we must NOT change
   its parent. We create a new account under the right parent and point the old
   one to it with chow_current_account. Otherwise we re-parent directly.
5. A CRM account under Bellhaven that is not on the website is never deleted or
   re-parented on a guess. It is flagged "Needs Review" (or handled as a change
   of ownership if the evidence is strong, like Sandusky).
"""
import difflib
import hashlib
import json
import re
from datetime import date

# ---------------------------------------------------------------------------
# Cleaning helpers
# ---------------------------------------------------------------------------
_STREET_WORDS = {
    "street": "st", "st": "st", "avenue": "ave", "ave": "ave", "av": "ave",
    "road": "rd", "rd": "rd", "boulevard": "blvd", "blvd": "blvd",
    "drive": "dr", "dr": "dr", "lane": "ln", "ln": "ln",
    "pike": "pike", "pk": "pike", "pke": "pike",
    "court": "ct", "ct": "ct", "circle": "cir", "cir": "cir",
    "place": "pl", "pl": "pl", "parkway": "pkwy", "pkwy": "pkwy",
    "highway": "hwy", "hwy": "hwy", "route": "rt", "rt": "rt",
    "terrace": "ter", "ter": "ter",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northwest": "nw", "northeast": "ne", "southwest": "sw", "southeast": "se",
}
_NAME_WORDS = {"rehab": "rehabilitation", "centre": "center", "healthcare": "health care",
               "&": "and"}
_GENERIC = {"bellhaven", "of", "at", "the", "and", "senior", "living", "care", "center",
            "health", "rehabilitation", "nursing", "assisted", "memory", "support",
            "community", "communities", "home", "retirement"}

CARE_MAP = {  # website wording -> CRM care_type
    "assisted living": "Assisted Living",
    "memory support": "Memory Care",
    "memory care": "Memory Care",
    "short-term rehabilitation & nursing": "Skilled Nursing",
    "skilled nursing": "Skilled Nursing",
    "independent living": "Independent Living",
}


def norm_street(street):
    s = (street or "").lower().replace(".", "")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(_STREET_WORDS.get(t, t) for t in s.split())


def is_po_box(street):
    return bool(re.match(r"^\s*(p\.?\s*o\.?\s*box|post office box)\b", (street or "").lower()))


def zip5(z):
    m = re.search(r"\d{5}", z or "")
    return m.group(0) if m else ""


def norm_name(name):
    s = (name or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    toks = [t for t in s.split() if t != "the"]
    out = []
    for t in toks:
        out.extend(_NAME_WORDS.get(t, t).split())
    return " ".join(out)


def name_tokens(name):
    return {t for t in norm_name(name).split() if t not in _GENERIC}


def jaccard(a, b):
    return len(a & b) / len(a | b) if (a | b) else 0.0


def digits(p):
    return re.sub(r"\D", "", p or "")


def care_for_crm(offerings):
    """Map website care offerings to the CRM's single care_type values."""
    mapped = []
    for o in offerings:
        v = CARE_MAP.get(o.strip().lower())
        if v and v not in mapped:
            mapped.append(v)
    return mapped


def has_billing(acct):
    """Billing SOP test: revenue history AND outstanding AR, both greater than zero."""
    return float(acct.get("lifetime_revenue") or 0) > 0 and float(acct.get("outstanding_ar") or 0) > 0


def has_any_billing(acct):
    return float(acct.get("lifetime_revenue") or 0) > 0 or float(acct.get("outstanding_ar") or 0) > 0


def same_place(loc, acct):
    """Return (strength, reason) or (None, ''). strength is 'strong' or 'weak'."""
    ls, as_ = norm_street(loc["street"]), norm_street(acct["billing_street"])
    lz, az = zip5(loc["zip"]), zip5(acct["billing_zip"])
    same_city = (loc["city"].strip().lower() == (acct["billing_city"] or "").strip().lower()
                 and loc["state"].strip().upper() == (acct["billing_state"] or "").strip().upper())
    if ls and ls == as_ and lz and lz == az:
        return "strong", "same street address and zip"
    if ls and ls == as_ and same_city:
        return "strong", f"same street and city (zip differs: website {lz}, CRM {az})"
    if same_city and norm_name(loc["name"]) == norm_name(acct["name"]):
        why = "CRM address is a PO Box" if is_po_box(acct["billing_street"]) else "CRM street differs"
        return "strong", f"same name and city ({why})"
    if same_city:
        lt, at = name_tokens(loc["name"]), name_tokens(acct["name"])
        if lt and at and jaccard(lt, at) >= 0.5:
            return "weak", "same city and similar name, but the street address is different"
    return None, ""


def proposal_key(ptype, slug, account_ids, fields):
    """Stable id for a proposal. Same facts => same key. The note text is left out on purpose."""
    core = {"type": ptype, "slug": slug, "accounts": sorted(account_ids),
            "fields": sorted((k, str(v)) for k, v in fields.items() if not k.endswith(".note"))}
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()[:12]


def _append_note(existing, text):
    existing = (existing or "").strip()
    return text if not existing else f"{existing} | {text}"


# ---------------------------------------------------------------------------
# The matcher
# ---------------------------------------------------------------------------
def find_parent_id(accounts, parent_name_hint="bellhaven"):
    for a in accounts:
        n = a["name"].lower()
        if parent_name_hint in n and "(parent account)" in n:
            return a["account_id"]
    raise ValueError("Could not find the Bellhaven parent account in the CRM data")


def _about_evidence(old_parent_name, about_text):
    """If the website's About page mentions the old parent, quote that sentence."""
    if not about_text or not old_parent_name:
        return None
    word = old_parent_name.split()[0]
    for sentence in re.split(r"(?<=[.!?])\s+", about_text):
        if word.lower() in sentence.lower():
            return f'Website About page says: "{sentence.strip()}"'
    return None


def build_proposals(locations, accounts, bellhaven_id=None, about_text="",
                    allow_missing=True, today=None):
    """Compare website locations to CRM accounts. Returns (proposals, stats)."""
    today = today or date.today().isoformat()
    bellhaven_id = bellhaven_id or find_parent_id(accounts)
    by_id = {a["account_id"]: a for a in accounts}
    parent_names = {a["account_id"]: a["name"] for a in accounts if not a["parent_id"]}
    proposals, stats = [], {"confident_matches": 0, "locations": len(locations)}
    # Accounts already retired by an earlier decision (marked as a duplicate, or pointing to a
    # successor account after a change of ownership) are finished business. They are never
    # matched or proposed again, which is what keeps re-runs quiet.
    retired = {a["account_id"] for a in accounts
               if a.get("duplicate_of_account") or a.get("chow_current_account")}
    claimed = set(retired)  # accounts already explained (survivor, duplicate loser, or retired)

    def pname(acct):
        return acct.get("parent_name") or parent_names.get(acct.get("parent_id"), "") or "no parent"

    def make(ptype, loc, accounts_involved, title, confidence, evidence, changes, actions, warnings=None, sop=None):
        fields = {}
        for act in actions:
            for k, v in act["fields"].items():
                fields[f"{act.get('account_id') or act.get('ref')}.{k}"] = v
        key = proposal_key(ptype, loc["slug"] if loc else None, accounts_involved, fields)
        proposals.append({
            "key": key, "type": ptype, "title": title, "confidence": confidence,
            "location": ({k: loc[k] for k in ("slug", "name", "street", "city", "state", "zip",
                                              "care_offerings", "phone", "url")} if loc else None),
            "account_id": accounts_involved[0] if accounts_involved else None,
            "evidence": evidence, "warnings": warnings or [], "changes": changes,
            "actions": actions, "sop": sop,
        })

    def change_row(acct, field, new):
        return {"account_id": acct["account_id"], "account_name": acct["name"],
                "field": field, "old": acct.get(field, ""), "new": new}

    # ---- pass 1: every website location --------------------------------------
    for loc in locations:
        strong, weak = [], []
        for acct in accounts:
            if acct["account_id"] == bellhaven_id or acct["account_id"] in claimed:
                continue
            strength, why = same_place(loc, acct)
            if strength == "strong":
                strong.append((acct, why))
            elif strength == "weak":
                weak.append((acct, why))

        new_fields = {
            "name": loc["name"], "billing_street": loc["street"], "billing_city": loc["city"],
            "billing_state": loc["state"], "billing_zip": loc["zip"], "parent_id": bellhaven_id,
            "status": "Active", "phone": loc["phone"],
        }
        mapped_care = care_for_crm(loc["care_offerings"])
        if mapped_care:
            new_fields["care_type"] = mapped_care[0]

        # -- no account at all, or only a weak lookalike ---
        if not strong:
            if weak:
                cand, why = max(weak, key=lambda w: jaccard(name_tokens(loc["name"]), name_tokens(w[0]["name"])))
                if cand["status"] == "Needs Review":
                    continue  # already flagged on an earlier run
                note = _append_note(cand.get("note"), (
                    f"Possible match to Bellhaven website location '{loc['name']}' "
                    f"({loc['street']}, {loc['city']}) but the address differs. "
                    f"Verify before linking. Flagged by bellhaven-sync on {today}."))
                make("review", loc, [cand["account_id"]],
                     f"Possible match: '{cand['name']}' may be '{loc['name']}'", "low",
                     [f"Website: {loc['name']}, {loc['street']}, {loc['city']}, {loc['state']} {loc['zip']}",
                      f"CRM: {cand['name']}, {cand['billing_street']}, {cand['billing_city']}, "
                      f"{cand['billing_state']} {cand['billing_zip']} (parent: {pname(cand)})",
                      f"Why it looks similar: {why}",
                      "Not enough to link them automatically, and creating a new account here could make a "
                      "duplicate. A person should decide."],
                     [change_row(cand, "status", "Needs Review"), change_row(cand, "note", note)],
                     [{"op": "patch", "account_id": cand["account_id"],
                       "fields": {"status": "Needs Review", "note": note},
                       "expect": {"status": cand["status"], "note": cand.get("note", "")}}])
            else:
                create_fields = dict(new_fields)
                create_fields["note"] = f"Created by bellhaven-sync from website page {loc['url']} on {today}."
                make("create", loc, [], f"Create new account: {loc['name']}", "high",
                     [f"Website lists {loc['name']}, {loc['street']}, {loc['city']}, {loc['state']} {loc['zip']}",
                      "No CRM account has this address, this name in this city, or a similar name in this city."],
                     [{"account_id": "(new)", "account_name": loc["name"], "field": k, "old": "", "new": v}
                      for k, v in create_fields.items() if k != "note"],
                     [{"op": "create", "ref": "new1", "fields": create_fields}])
            continue

        # -- one or more accounts are the same place ---
        def rank(item):
            a, _ = item
            return (not has_any_billing(a),                     # keep accounts with billing history
                    a["parent_id"] != bellhaven_id,             # prefer already under Bellhaven
                    digits(a["phone"]) != digits(loc["phone"]),  # prefer matching phone
                    norm_name(a["name"]) != norm_name(loc["name"]),
                    not a["parent_id"],                         # prefer accounts that have a parent
                    a["account_id"])
        strong.sort(key=rank)
        survivor, why = strong[0]
        losers = strong[1:]
        claimed.add(survivor["account_id"])
        phone_ok = digits(survivor["phone"]) == digits(loc["phone"])
        confidence = "high" if (why.startswith("same street address") or phone_ok) else "medium"

        # duplicates
        for loser, lwhy in losers:
            claimed.add(loser["account_id"])
            note = _append_note(loser.get("note"), (
                f"Duplicate of {survivor['account_id']} ({survivor['name']}); same place as Bellhaven website "
                f"location '{loc['name']}'. Marked Inactive by bellhaven-sync on {today}."))
            ev = [f"Same place as {survivor['name']} ({survivor['account_id']}): {lwhy}.",
                  f"Survivor chosen because: " + _survivor_reason(survivor, strong, loc, bellhaven_id),
                  f"Loser's parent is {pname(loser)}; its parent is left unchanged."]
            warn = []
            if has_any_billing(loser):
                warn.append(f"This account has billing history (revenue {loser['lifetime_revenue']}, "
                            f"AR {loser['outstanding_ar']}). Check with billing before approving.")
            if rank((survivor, why))[:5] == rank((loser, lwhy))[:5]:
                warn.append("All candidates had equal evidence; the survivor was picked by a fixed tie-break "
                            "(lowest account id). Reject this if you prefer the other account.")
            fields = {"status": "Inactive", "duplicate_of_account": survivor["account_id"], "note": note}
            make("duplicate", loc, [loser["account_id"], survivor["account_id"]],
                 f"Duplicate: '{loser['name']}' is a copy of '{survivor['name']}'", "high" if not warn else "medium",
                 ev, [change_row(loser, "status", "Inactive"),
                      change_row(loser, "duplicate_of_account", survivor["account_id"]),
                      change_row(loser, "note", note)],
                 [{"op": "patch", "account_id": loser["account_id"], "fields": fields,
                   "expect": {"status": loser["status"], "duplicate_of_account": loser.get("duplicate_of_account", "")}}],
                 warn)

        # field fixes on the survivor
        evidence = [f"Website: {loc['name']}, {loc['street']}, {loc['city']}, {loc['state']} {loc['zip']}",
                    f"CRM: {survivor['name']}, {survivor['billing_street']}, {survivor['billing_city']}, "
                    f"{survivor['billing_state']} {survivor['billing_zip']} (parent: {pname(survivor)})",
                    f"Matched because: {why}."]
        if phone_ok:
            evidence.append("The phone number also matches.")
        fields, changes = {}, []

        if survivor["name"].strip() != loc["name"].strip():
            fields["name"] = loc["name"]
            evidence.append("The website name is treated as current; the CRM name looks outdated or differently written.")
        if norm_street(survivor["billing_street"]) != norm_street(loc["street"]):
            fields["billing_street"] = loc["street"]
        if zip5(survivor["billing_zip"]) != zip5(loc["zip"]):
            fields["billing_zip"] = loc["zip"]
        if (survivor["billing_city"] or "").strip().lower() != loc["city"].strip().lower():
            fields["billing_city"] = loc["city"]
        if (survivor["billing_state"] or "").strip().upper() != loc["state"].strip().upper():
            fields["billing_state"] = loc["state"]
        if mapped_care and survivor["care_type"] not in mapped_care and survivor["care_type"]:
            if len(mapped_care) == 1:
                fields["care_type"] = mapped_care[0]
        if survivor["status"] != "Active":
            fields["status"] = "Active"
            evidence.append("The website lists this community but the CRM shows it as not Active.")

        sop = None
        if survivor["parent_id"] != bellhaven_id:
            old_parent = pname(survivor)
            about = _about_evidence(old_parent, about_text)
            sop = {"revenue": survivor["lifetime_revenue"], "ar": survivor["outstanding_ar"],
                   "path": "chow" if has_billing(survivor) else "direct"}
            if sop["path"] == "chow":
                create_fields = dict(new_fields)
                create_fields["note"] = (f"Created by bellhaven-sync on {today}: successor account after change of "
                                         f"ownership from {survivor['account_id']} ({survivor['name']}).")
                ev = evidence + [
                    f"This place is listed by Bellhaven but the CRM parent is {old_parent}.",
                    f"BILLING SOP: revenue {survivor['lifetime_revenue']} and outstanding AR "
                    f"{survivor['outstanding_ar']} are both above zero, so the old account must be preserved "
                    "and its parent must NOT change.",
                    "Plan: create a new account under Bellhaven, then set chow_current_account on the OLD account "
                    "to the new account's id. Nothing else on the old account changes."]
                if about:
                    ev.append(about)
                make("chow", loc, [survivor["account_id"]],
                     f"Change of ownership: '{survivor['name']}' moves to Bellhaven (billing SOP)", "high", ev,
                     [{"account_id": "(new)", "account_name": loc["name"], "field": k, "old": "", "new": v}
                      for k, v in create_fields.items() if k != "note"] +
                     [change_row(survivor, "chow_current_account", "(id of the new account)")],
                     [{"op": "create", "ref": "new1", "fields": create_fields},
                      {"op": "patch", "account_id": survivor["account_id"],
                       "fields": {"chow_current_account": "$ref:new1"},
                       "expect": {"chow_current_account": survivor.get("chow_current_account", ""),
                                  "parent_id": survivor["parent_id"]}}],
                     sop=sop)
                continue
            fields["parent_id"] = bellhaven_id
            evidence.append(f"This place is listed by Bellhaven but the CRM parent is {old_parent}.")
            evidence.append("BILLING SOP check: " + (
                "no outstanding AR" if float(survivor.get("outstanding_ar") or 0) <= 0 else "no revenue history")
                + f" (revenue {survivor['lifetime_revenue']}, AR {survivor['outstanding_ar']}), so it is safe to re-parent directly.")
            if about:
                evidence.append(about)

        if not fields:
            stats["confident_matches"] += 1
            continue
        for k, v in fields.items():
            changes.append(change_row(survivor, k, v))
        make("update", loc, [survivor["account_id"]],
             f"Fix '{survivor['name']}': " + ", ".join(_field_label(k) for k in fields), confidence, evidence,
             changes,
             [{"op": "patch", "account_id": survivor["account_id"], "fields": fields,
               "expect": {k: survivor.get(k, "") for k in fields}}], sop=sop)

    # ---- pass 2: Bellhaven accounts that are not on the website ----------------
    if allow_missing:
        for acct in accounts:
            if acct["parent_id"] != bellhaven_id or acct["account_id"] in claimed:
                continue
            claimed.add(acct["account_id"])
            twin = next((o for o in accounts
                         if o["account_id"] not in (acct["account_id"], bellhaven_id)
                         and o["account_id"] not in claimed
                         and norm_street(o["billing_street"]) == norm_street(acct["billing_street"])
                         and zip5(o["billing_zip"]) == zip5(acct["billing_zip"])
                         and o["billing_street"] and o["parent_id"] != bellhaven_id), None)
            base_ev = [f"CRM: {acct['name']}, {acct['billing_street']}, {acct['billing_city']}, "
                       f"{acct['billing_state']} {acct['billing_zip']}, under Bellhaven.",
                       "No Bellhaven website page has this address or a matching name in this city."]
            if twin is not None:
                tp = pname(twin)
                ev = base_ev + [f"Another CRM account at the same address, '{twin['name']}', sits under {tp}. "
                                f"That points to a sale of this community to {tp}."]
                if has_billing(acct):
                    sop = {"revenue": acct["lifetime_revenue"], "ar": acct["outstanding_ar"], "path": "chow"}
                    ev.append(f"BILLING SOP: revenue {acct['lifetime_revenue']} and outstanding AR "
                              f"{acct['outstanding_ar']} are both above zero, so this account's parent must NOT "
                              "change and the account stays exactly as it is.")
                    ev.append("The correct new account already exists, so no new account is created. We only set "
                              "chow_current_account on the old account to point to it.")
                    make("chow", None, [acct["account_id"], twin["account_id"]],
                         f"Change of ownership: '{acct['name']}' now belongs to {tp} (billing SOP)", "medium", ev,
                         [change_row(acct, "chow_current_account", twin["account_id"])],
                         [{"op": "patch", "account_id": acct["account_id"],
                           "fields": {"chow_current_account": twin["account_id"]},
                           "expect": {"chow_current_account": acct.get("chow_current_account", ""),
                                      "parent_id": acct["parent_id"]}}], sop=sop)
                else:
                    note = _append_note(acct.get("note"), (
                        f"Duplicate of {twin['account_id']} ({twin['name']}, {tp}); no longer on the Bellhaven "
                        f"website. Marked Inactive by bellhaven-sync on {today}."))
                    make("duplicate", None, [acct["account_id"], twin["account_id"]],
                         f"Duplicate: '{acct['name']}' is a copy of '{twin['name']}' ({tp})", "medium",
                         ev + ["No billing history, so this can be retired safely."],
                         [change_row(acct, "status", "Inactive"),
                          change_row(acct, "duplicate_of_account", twin["account_id"]),
                          change_row(acct, "note", note)],
                         [{"op": "patch", "account_id": acct["account_id"],
                           "fields": {"status": "Inactive", "duplicate_of_account": twin["account_id"], "note": note},
                           "expect": {"status": acct["status"], "duplicate_of_account": acct.get("duplicate_of_account", "")}}])
                continue
            if acct["status"] == "Needs Review":
                continue  # already flagged on an earlier run
            billing = (f" It has billing history (revenue {acct['lifetime_revenue']}, AR {acct['outstanding_ar']}), "
                       "so do NOT change its parent." if has_any_billing(acct) else "")
            note = _append_note(acct.get("note"), (
                f"Not listed on the Bellhaven website as of {today}. Verify whether it closed or was sold."
                f"{billing} Flagged by bellhaven-sync."))
            warn = ["This account has billing history. Its parent is intentionally left unchanged."] if has_any_billing(acct) else []
            make("missing", None, [acct["account_id"]],
                 f"Not on website: '{acct['name']}'", "medium",
                 base_ev + ["Absence from a website is not proof of closure or sale, so the account is flagged, "
                            "not deactivated or moved."],
                 [change_row(acct, "status", "Needs Review"), change_row(acct, "note", note)],
                 [{"op": "patch", "account_id": acct["account_id"],
                   "fields": {"status": "Needs Review", "note": note},
                   "expect": {"status": acct["status"], "note": acct.get("note", "")}}], warn)

    stats["proposals"] = len(proposals)
    return proposals, stats


def _field_label(field):
    return {"name": "name", "billing_street": "street", "billing_zip": "zip", "billing_city": "city",
            "billing_state": "state", "parent_id": "parent", "status": "status", "care_type": "care type"}.get(field, field)


def _survivor_reason(survivor, group, loc, bellhaven_id):
    reasons = []
    if has_any_billing(survivor):
        reasons.append("it has billing history")
    if survivor["parent_id"] == bellhaven_id:
        reasons.append("it is already under Bellhaven")
    if digits(survivor["phone"]) == digits(loc["phone"]):
        reasons.append("its phone matches the website")
    if norm_name(survivor["name"]) == norm_name(loc["name"]):
        reasons.append("its name matches the website")
    if not reasons and survivor["parent_id"]:
        reasons.append("it has a parent account recorded")
    return ", ".join(reasons) if reasons else "tie-break"

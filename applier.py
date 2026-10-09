"""Step 4: writing an APPROVED proposal back to the CRM.

Safety rules built in:
  * Only proposals the human approved are ever applied.
  * Before changing an account we re-read it. If it no longer looks the way it did when
    the proposal was made, we STOP (someone changed it; run the pipeline again).
  * After changing an account we re-read it and check the change really stuck.
  * Multi-step proposals (create a new account, then point the old one at it) remember
    their progress. If step 2 fails, a retry resumes at step 2 and does not create a
    second account.
"""
from crm import CRMError
from matcher import norm_street, zip5


class ApplyError(Exception):
    pass


def _resolve(fields, created):
    out = {}
    for key, value in fields.items():
        if isinstance(value, str) and value.startswith("$ref:"):
            ref = value[len("$ref:"):]
            if ref not in created:
                raise ApplyError(f"internal error: step refers to '{ref}' which was not created yet")
            value = created[ref]
        out[key] = value
    return out


def _same(a, b):
    return str("" if a is None else a) == str("" if b is None else b)


def _find_existing_twin(crm, fields):
    """Before creating, check whether an identical account already exists (for example from a
    crashed earlier attempt), so a retry never makes a duplicate."""
    rows = crm.list_accounts(zip=zip5(fields.get("billing_zip", "")))
    for row in rows:
        if (norm_street(row["billing_street"]) == norm_street(fields["billing_street"])
                and zip5(row["billing_zip"]) == zip5(fields["billing_zip"])
                and row["parent_id"] == fields.get("parent_id")):
            return row
    return None


def apply_entry(crm, ledger, key):
    """Apply one ledger entry. Returns True on success, False on failure (error saved)."""
    entry = ledger.entries[key]
    if entry["status"] not in ("approved", "failed"):
        raise ApplyError(f"proposal {key} is '{entry['status']}', only approved proposals can be applied")
    proposal = entry["proposal"]
    progress = entry["progress"]
    created = progress["created"]
    entry["error"] = ""
    try:
        for index, action in enumerate(proposal["actions"]):
            if index in progress["done"]:
                continue
            if action["op"] == "create":
                twin = _find_existing_twin(crm, action["fields"])
                if twin is not None:
                    created[action["ref"]] = twin["account_id"]
                    ledger.log(key, f"step {index + 1}: found existing matching account {twin['account_id']}, not creating another")
                else:
                    wanted = _resolve(action["fields"], created)
                    new_acct = crm.create_account(wanted)
                    created[action["ref"]] = new_acct["account_id"]
                    ledger.log(key, f"step {index + 1}: created account {new_acct['account_id']}")
                    after = crm.get_account(new_acct["account_id"])
                    wrong = [k for k in ("name", "parent_id") if not _same(after.get(k), wanted.get(k))]
                    if wrong:
                        raise ApplyError(f"new account {new_acct['account_id']}: the CRM did not keep: {', '.join(wrong)}")
                progress["done"].append(index)
                ledger.save()
            elif action["op"] == "patch":
                account_id = action["account_id"]
                target = _resolve(action["fields"], created)
                current = crm.get_account(account_id)
                if all(_same(current.get(k), v) for k, v in target.items()):
                    ledger.log(key, f"step {index + 1}: {account_id} already has the target values")
                else:
                    for field, expected in action.get("expect", {}).items():
                        if not _same(current.get(field), expected):
                            raise ApplyError(
                                f"{account_id} changed since this proposal was made ({field} is now "
                                f"'{current.get(field)}', expected '{expected}'). Nothing was written for this step. "
                                "Run the pipeline again to get fresh proposals.")
                    crm.patch_account(account_id, target)
                    after = crm.get_account(account_id)
                    wrong = [k for k, v in target.items() if not _same(after.get(k), v)]
                    if wrong:
                        raise ApplyError(f"{account_id}: the CRM did not keep these fields: {', '.join(wrong)}")
                    ledger.log(key, f"step {index + 1}: updated {account_id} ({', '.join(target)})")
                progress["done"].append(index)
                ledger.save()
            else:
                raise ApplyError(f"unknown action {action['op']}")
        ledger.set_status(key, "applied", "applied and verified")
        return True
    except (ApplyError, CRMError) as exc:
        entry["error"] = str(exc)
        ledger.set_status(key, "failed", f"failed: {exc}")
        return False


def approve_and_apply(crm, ledger, key):
    """What the Approve button does: record the approval, then write it."""
    entry = ledger.entries[key]
    if entry["status"] not in ("pending", "failed"):
        raise ApplyError(f"cannot approve a proposal that is '{entry['status']}'")
    ledger.set_status(key, "approved", "approved by reviewer")
    return apply_entry(crm, ledger, key)


def reject(ledger, key):
    if ledger.entries[key]["status"] in ("applied",):
        raise ApplyError("an applied proposal cannot be rejected")
    ledger.set_status(key, "rejected", "rejected by reviewer")


def reset(ledger, key):
    if ledger.entries[key]["status"] != "rejected":
        raise ApplyError("only a rejected proposal can be reset to pending")
    ledger.entries[key]["progress"] = {"created": {}, "done": []}
    ledger.set_status(key, "pending", "reset to pending by reviewer")


def apply_all_approved(crm, ledger):
    results = {"applied": 0, "failed": 0}
    for key, entry in list(ledger.entries.items()):
        if entry["status"] == "approved":
            results["applied" if apply_entry(crm, ledger, key) else "failed"] += 1
    return results

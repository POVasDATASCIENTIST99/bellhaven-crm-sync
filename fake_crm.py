"""An in-memory pretend CRM with the same methods as crm.CRMClient.

Used by the tests, and by `python sync.py serve --offline` so you can rehearse the
whole review-and-approve flow without touching the real sandbox.
"""
import copy

from crm import CRMError

WRITABLE = {"name", "parent_id", "billing_street", "billing_city", "billing_state", "billing_zip",
            "care_type", "status", "phone", "note", "chow_current_account", "duplicate_of_account"}
VALID_STATUS = {"Active", "Inactive", "Needs Review"}


class FakeCRM:
    def __init__(self, accounts):
        self.accounts = {a["account_id"]: copy.deepcopy(a) for a in accounts}
        self._n = 0
        self.log = []          # every write, in order, for tests to inspect
        self.fail_patch_for = set()  # account ids whose PATCH should fail (tests)

    def _parent_name(self, parent_id):
        parent = self.accounts.get(parent_id)
        return parent["name"] if parent else ""

    FILTER_FIELDS = {"zip": "billing_zip", "city": "billing_city", "state": "billing_state",
                     "street": "billing_street", "parent_id": "parent_id"}

    def list_accounts(self, **filters):
        rows = list(self.accounts.values())
        for k, v in filters.items():
            if v:
                field = self.FILTER_FIELDS.get(k, k)
                rows = [a for a in rows if str(a.get(field, "")).lower() == str(v).lower()]
        return copy.deepcopy(rows)

    def get_account(self, account_id):
        if account_id not in self.accounts:
            raise CRMError(f"GET /accounts/{account_id} failed with HTTP 404", 404)
        return copy.deepcopy(self.accounts[account_id])

    def _check(self, fields):
        bad = set(fields) - WRITABLE
        if bad:
            raise CRMError(f"HTTP 422: unknown fields {sorted(bad)}", 422)
        if "status" in fields and fields["status"] not in VALID_STATUS:
            raise CRMError("HTTP 422: invalid status", 422)

    def patch_account(self, account_id, fields):
        if account_id in self.fail_patch_for:
            raise CRMError(f"PATCH /accounts/{account_id} failed with HTTP 500", 500)
        acct = self.accounts.get(account_id)
        if acct is None:
            raise CRMError(f"PATCH /accounts/{account_id} failed with HTTP 404", 404)
        self._check(fields)
        acct.update(fields)
        if "parent_id" in fields:
            acct["parent_name"] = self._parent_name(fields["parent_id"])
        self.log.append(("PATCH", account_id, dict(fields)))
        return copy.deepcopy(acct)

    def create_account(self, fields):
        self._check(fields)
        self._n += 1
        new_id = f"001NEWACCT{self._n:07d}"
        acct = {"account_id": new_id, "name": "", "parent_id": "", "parent_name": "", "billing_street": "",
                "billing_city": "", "billing_state": "", "billing_zip": "", "care_type": "", "status": "Active",
                "phone": "", "lifetime_revenue": 0, "outstanding_ar": 0, "chow_current_account": "",
                "duplicate_of_account": "", "note": "", "created_by_candidate": True, "updated_at": ""}
        acct.update(fields)
        acct["parent_name"] = self._parent_name(acct["parent_id"])
        self.accounts[new_id] = acct
        self.log.append(("POST", new_id, dict(fields)))
        return copy.deepcopy(acct)

"""The decision ledger: the memory that makes re-runs safe.

Every proposal has a stable key (see matcher.proposal_key). The ledger remembers,
for each key, what the human decided. On a re-run:
  - a key that is already approved/applied/rejected is NOT proposed again,
  - a key that is still pending stays one single pending item (no duplicates),
  - a pending key that is no longer produced (the CRM or website changed) becomes "stale",
  - a key that was applied but shows up again (someone undid the fix) goes back to pending.
"""
import json
import os
import tempfile
from datetime import datetime, timezone

STATUSES = ("pending", "approved", "rejected", "applied", "failed", "stale")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Ledger:
    def __init__(self, path):
        self.path = path
        self.data = {"version": 1, "proposals": {}, "runs": []}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                self.data = json.load(fh)

    @property
    def entries(self):
        return self.data["proposals"]

    def save(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(self.path)), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def log(self, key, event):
        self.entries[key]["history"].append({"at": now(), "event": event})

    def set_status(self, key, status, event=""):
        assert status in STATUSES, status
        entry = self.entries[key]
        entry["status"] = status
        if event:
            self.log(key, event)
        self.save()

    def merge(self, proposals):
        """Fold a fresh list of proposals into the ledger. Returns a summary of what happened."""
        summary = {"new": 0, "still_pending": 0, "already_decided": 0, "reappeared": 0, "stale": 0}
        seen = set()
        for p in proposals:
            key = p["key"]
            seen.add(key)
            entry = self.entries.get(key)
            if entry is None:
                self.entries[key] = {"proposal": p, "status": "pending", "created_at": now(),
                                     "history": [{"at": now(), "event": "proposed"}],
                                     "progress": {"created": {}, "done": []}, "error": ""}
                summary["new"] += 1
            elif entry["status"] in ("pending", "failed", "approved"):
                if entry["status"] == "pending":
                    entry["proposal"] = p  # refresh the evidence text only
                summary["still_pending"] += 1
            elif entry["status"] in ("stale", "applied"):
                entry["status"] = "pending"
                entry["proposal"] = p
                entry["progress"] = {"created": {}, "done": []}
                entry["error"] = ""
                self.log(key, "reappeared after being " + ("applied" if entry["history"][-1]["event"].startswith("applied") else "stale"))
                summary["reappeared"] += 1
            else:  # rejected: respect the earlier decision
                summary["already_decided"] += 1
        for key, entry in self.entries.items():
            if key not in seen and entry["status"] in ("pending", "approved", "failed"):
                entry["status"] = "stale"
                self.log(key, "no longer proposed (CRM or website changed); will not be applied")
                summary["stale"] += 1
        self.data["runs"].append({"at": now(), "proposals_seen": len(proposals), **summary})
        self.save()
        return summary

    def counts(self):
        out = {s: 0 for s in STATUSES}
        for entry in self.entries.values():
            out[entry["status"]] += 1
        return out

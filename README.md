# Bellhaven CRM sync

Keeps the CRM's facility-to-parent links honest by comparing Bellhaven Senior Living's
public website (the source of truth for what exists) with the CRM sandbox.

No installs needed. Python 3.10 or newer, built-in libraries only.

## The four steps (and the file for each)

| Step | What happens | File |
|---|---|---|
| 1. Scrape | Reads every Bellhaven community page: name, street, city, state, zip, care offerings | `scraper.py` |
| 2. Match | Compares each website location with the CRM and makes **proposals** with evidence | `matcher.py` |
| 3. Review | A local web page: a person approves or rejects each proposal. Approve writes it to the CRM | `review_app.py`, `applier.py` |
| 4. Remember | Remembers every decision so re-runs never re-propose decided items | `store.py` |

`sync.py` ties them together. `crm.py` is the API client. `fake_crm.py` is a pretend CRM for tests and rehearsal.

## Run it

```
# PowerShell, once per window. The token never goes in a file.
$env:BH_TOKEN = "your token"

python sync.py check-api     # optional: proves the token and PATCH work, changes nothing
python sync.py run           # scrape + match. Writes NOTHING to the CRM
python sync.py serve         # then open http://127.0.0.1:8765 and review
```

Rehearse safely (nothing real is touched): `python sync.py serve --offline`

Run the tests: `python -m unittest discover -s tests -v`

## Daily schedule

- `.github/workflows/daily.yml` runs the tests, then `python sync.py run` every day at 06:00 UTC.
- `crontab.txt` is the same schedule as a cron line.

The daily job only refreshes the review queue. Writing to the CRM always waits for a person.
The decision ledger (`data/decisions.json`) is cached between GitHub runs. For long-term use it
should live somewhere durable (a database or a committed state branch).

## Safety rules built in

- **Nothing writes without approval.** Proposals are only data until Approve is clicked.
- **Billing SOP.** If an account has `lifetime_revenue > 0` AND `outstanding_ar > 0`, its parent is never
  changed. A new account is created under the right parent (or an existing one is reused), and the old
  account gets `chow_current_account` pointing to it. A test checks no proposal ever re-parents such an account.
- **Re-runs are quiet.** Each proposal has a stable key. Decided items are remembered; a fixed CRM produces no proposals.
- **Broken scrape protection.** If the website scrape looks broken, the last good data is kept and
  "not on website" checks are skipped, so an outage can never look like "everything closed".
- **Stale protection.** Before writing, the app re-reads the account. If it changed since the proposal, it refuses.
- **Verified writes.** After writing, the app re-reads the account and checks the change stuck.
- **Resumable.** If a two-step change fails halfway, retrying continues where it stopped and never creates a second account.

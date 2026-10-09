# Write-up: Bellhaven CRM sync

**Time spent:** about 5 hours 30 minutes in total, including reading the task, directing the AI, reviewing every proposal, approving the changes, and uploading to GitHub.

## 1. What it does

A four-step pipeline keeps the CRM's facility-to-parent links accurate.

1. **Scrape** (`scraper.py`): reads the 3 directory pages, the home page, and every community page (35 communities:
   name, street, city, state, zip, care offerings, plus phone, administrator and any notice banner).
2. **Match** (`matcher.py`): compares each website location with the 121 CRM accounts and creates **proposals**,
   each with evidence. It never writes anything.
3. **Review** (`review_app.py`): a local web page. A person sees each proposal and its evidence and approves or
   rejects it. Approve writes it to the CRM through the API and then re-reads the account to confirm.
4. **Remember** (`store.py`): a ledger of every decision, so a second run does not re-propose decided items.

A daily schedule is in `.github/workflows/daily.yml` (and `crontab.txt`). It runs the tests, then scrapes and
refreshes the queue. It never writes to the CRM; a person approves first.

## 2. How two records are judged to be the same place

Names are unreliable here (rebrands and stale names are the whole problem), so the address leads:

- **Strong:** same street and zip after cleaning (Boulevard = Blvd, Northwest = NW, Pk = Pike), or same street and
  city with a different zip, or same name and city when the CRM address is a PO Box.
- **Weak:** same city and a similar name but a different street. Weak is never linked automatically.
- Name-only look-alikes are rejected on purpose: "Bellhaven of Carlisle" (PA) is not "Bellhaven of New Carlisle" (OH),
  and "Amberly Manor" in Hudson, OH is not "Amberly Manor" in Colorado Springs.

## 3. What it found (35 website locations, 121 accounts)

| Result | Count | Examples |
|---|---|---|
| Already correct | 16 | Maplewood, Akron, Goshen |
| Rebrand or outdated name -> rename | 7 | "Riverbend Manor Care Center" is now Bellhaven of Chagrin Falls |
| Address typo or PO Box -> fix | 2 | Portsmouth zip 45626 -> 45662; Ashtabula PO Box -> street |
| Wrong parent, safe -> re-parent | 4 | Lima (Harborview), Zanesville (Cedar Trail), Findlay (no parent), Kettering |
| Wrong parent, has billing -> **change of ownership** | 2 | Tiffin, Marietta |
| Duplicates -> Inactive + `duplicate_of_account` | 7 | Owosso, Monroe (3 records), Port Clinton, Erie, Kettering (3 records) |
| No CRM account -> create | 3 | Batavia, Carlisle, Amberly Manor |
| Under Bellhaven, gone from website -> flag | 2 | Alliance, Coldwater |
| Sold to another parent, has billing -> change of ownership | 1 | Sandusky |
| Possible match, needs a person | 1 | Union Square |

(The rename and re-parent rows overlap for Zanesville and Kettering, which need both.)

## 4. The billing SOP

An account whose `lifetime_revenue > 0` **and** `outstanding_ar > 0` is never re-parented. For Tiffin
(revenue 84,000, AR 12,400) and Marietta (revenue 51,250, AR 3,800) the plan is: create a new account under
Bellhaven from the website data, then set `chow_current_account` on the old account to the new id. Nothing else on
the old account changes. Lima, Findlay and the others have no AR, so they are re-parented directly. A test
asserts that no proposal ever puts `parent_id` on an account that has billing.

## 5. Judgment calls I want to be upfront about

- **Sandusky.** It is under Bellhaven with billing (revenue 130,000, AR 5,200), is not on the website, and a
  second account at the same address already exists under Millstone Health Partners. I treated that as a sale.
  The SOP says to create a new account, but one already exists, so creating another would manufacture a duplicate.
  I only set `chow_current_account` on the old account to point at the existing one.
- **Alliance and Coldwater.** Missing from a website does not prove closure or sale, so they are set to
  `Needs Review` with a note, not `Inactive`, and their parent is untouched.
- **Duplicate survivors.** Ranked by: has billing history, already under Bellhaven, phone matches the website,
  name matches, has a parent. Kettering has three records with equal evidence, so the survivor is chosen by a fixed
  tie-break and the card says so. The losers keep their parent (I never re-parent a loser).
- **Union Square.** The CRM has "Union Square Senior Living" at a different street and phone. That is not enough
  to link, and creating a new account could duplicate it, so it is a low-confidence card for a person.
- **Phone numbers** disagree between the website and CRM even for clear matches, so they are evidence and a
  tie-break, never a reason to change a record.
- **Care types.** The website says "Short-Term Rehabilitation & Nursing" and "Memory Support"; the CRM says
  "Skilled Nursing" and "Memory Care". They are mapped; no real mismatches were found.
- **About page.** It says Bellhaven welcomed Harborview in 2025 and "select communities from Cedar Trail" in 2026.
  Re-parent cards quote this as supporting evidence.

## 6. Making re-runs safe

Each proposal has a stable key built from its type, the accounts involved and the values to be written (not the
note text or the date). The ledger remembers the decision for that key. Run twice with no decisions: nothing new.
Reject something: it stays rejected until the underlying facts change. Apply everything: the matcher itself finds
nothing left to say (tested). Accounts already marked as duplicates or pointing to a successor are never matched again.

## 7. How I checked the AI's work

- 49 automated tests, including tests run against the real data (every proposal type, the SOP, the decoy names,
  "apply everything then re-run gives zero proposals", resume after a failed step, refusing to write to a changed account).
- A safe test command (`python sync.py check-api`) and an offline rehearsal mode before touching the real CRM.
- I read the evidence on each proposal card myself and approved all 29 changes by hand in the review page; I rejected none.
  I made conscious calls on the uncertain ones: the Kettering survivor (a fixed tie-break, medium confidence), Sandusky
  (treated as a sale, medium confidence), Alliance and Coldwater (flagged for review, not deactivated), and Union Square
  (kept as a flag for a person). I also re-ran the tool after applying everything and confirmed it proposed nothing new.

## 8. Limits and next steps

- The API documentation does not describe request bodies, so field names for writes follow the shape of the read
  responses. `check-api` and the first approval confirm this; every write is verified by re-reading.
- Contacts attached to a duplicate are not moved to the survivor.
- The decision ledger is a local file (cached between GitHub runs). Production would use a database.
- Add notifications (for example a Slack message when new proposals appear) and a daily summary.
- Names are matched with simple rules, not an LLM. An LLM could help with rebrand evidence, but the address-based
  rules were enough and are easier to explain and test.

## 9. AI tools used

I used Claude (an AI assistant from Anthropic) to write the code from my instructions. I directed the work one step at a
time, ran the tool myself against the real CRM, and checked the results by reading each proposal's evidence and verifying
the accounts in the CRM afterwards. Claude also wrote the 49 tests. I do not claim to have typed the code line by line;
I can explain what each file does and make a live change in the walkthrough.

# Cheat sheet for the 45-minute live call (plain language)

## The 60-second story

"Clipboard sells to senior living facilities, and about 60 percent belong to a parent company. Owners change all the
time, and when they do the link between a facility and its parent in the CRM goes quietly wrong. I built a small
tool that reads Bellhaven's website, compares it with the CRM, and proposes fixes with evidence. A person approves
each fix, and only then does anything get written. It remembers decisions so running it daily never repeats work.
Accounts with money history are handled by a billing rule so we never lose the old record."

## The four buckets (use real names)

1. **Already correct:** Maplewood, Akron, Goshen. Nothing to do.
2. **Needs a fix:** "Riverbend Manor Care Center" is now "Bellhaven of Chagrin Falls" (rename); Portsmouth has a zip typo;
   Ashtabula has a PO Box instead of a street; Lima sits under the wrong parent (Harborview).
3. **No CRM account:** Batavia, Carlisle, Amberly Manor. They are created under Bellhaven.
4. **In the CRM under Bellhaven but gone from the website:** Alliance, Coldwater (flag for review) and Sandusky (sold
   to Millstone, has billing, so change-of-ownership).

## The billing rule in one breath

"If an account has revenue AND unpaid balance, finance needs it untouched. So I do not move it. I create (or reuse) an
account under the new parent and set `chow_current_account` on the old one. If it has no revenue or no unpaid
balance, I just move it. Tiffin and Marietta are the examples. Lima is the contrast: no unpaid balance, so it moved directly."
Where: `has_billing()` in `matcher.py`.

## The files, one line each

- `scraper.py`: visits every page, turns each page into a clean record, refuses to overwrite good data if the scrape looks broken.
- `matcher.py`: the brain. `same_place()` decides if a website location and CRM account are the same; `build_proposals()` makes the proposals.
- `store.py`: the memory of decisions. This is what makes re-runs safe.
- `applier.py`: writes an approved change, but first re-reads the account (refuses if it changed), then re-reads to confirm it stuck.
- `review_app.py`: the web page with Approve and Reject.
- `sync.py`: the one command (`run`, `serve`, `apply`, `status`, `check-api`).
- `crm.py`: talks to the API. The token comes only from `BH_TOKEN`.
- `tests/`: 49 tests, many on the real data.

## Suggested demo order

1. `python sync.py run` and show the summary line.
2. `python sync.py serve`, open the page, explain the sections from top to bottom.
3. Open one card of each kind (Tiffin, Lima, Owosso, Batavia, Alliance). Read the evidence out loud.
4. Approve one low-risk item. Show the green "Written and verified" message and the account in the CRM browser.
5. Run `python sync.py run` again and show: nothing new, rejected stays rejected.
6. Say the limits honestly (see the write-up, section 8).

## Likely "small live change" requests and where to edit

| They ask | Where |
|---|---|
| "Treat AR over 1000 as billing" | `has_billing()` in `matcher.py` |
| "Mark missing accounts Inactive instead of Needs Review" | the `missing` block at the end of `build_proposals()` |
| "Scrape one more field (for example administrator)" | `parse_detail()` already reads it; add it to `new_fields` in `build_proposals()` |
| "Change which duplicate survives" | `rank()` inside `build_proposals()` |
| "Run every 6 hours" | `cron:` line in `.github/workflows/daily.yml` (for example `0 */6 * * *`) |
| "Also compare phone numbers" | add a check next to the street and zip checks in `build_proposals()` |
| "Never auto-approve anything" | remove the group-approve button in `review_app.py` |

## Questions they may ask

- **Why not delete duplicates?** The API has no delete or merge; the convention is Inactive plus `duplicate_of_account`.
- **Why flag instead of deactivate missing accounts?** A missing web page is not proof of closure or sale.
- **Why match on address, not name?** Names are what goes stale in a rebrand. The test data has "Bellhaven of Carlisle" vs "Bellhaven of New Carlisle" as a trap.
- **What if the website is down?** The scraper keeps the last good file and the matcher skips the "not on website" checks.
- **What if someone edits the CRM between proposal and approval?** The app re-reads first and refuses; the card turns red.
- **How is a re-run safe?** Stable proposal keys plus the decision ledger; applied accounts stop matching because the CRM is now correct.
- **Why no LLM?** Address rules were enough, are testable, and are easy to explain. An LLM could help with ambiguous rebrands later.
- **What would you improve?** Move contacts from duplicates to survivors, a durable database for decisions, notifications, handle multi-owner chains.

## Be ready to admit

- The API docs do not show request bodies, so I confirmed writes with `check-api` and by verifying every write.
- Kettering's survivor is a tie-break; a person could reasonably pick another.
- Union Square is a judgment call that I deliberately left to a person.

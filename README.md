# TRID / LE Deadline Alert

Daily GitHub Action that emails loan officers about correspondent loans whose
Loan Estimate deadline is approaching or already past, with no LE sent date in
Vesta.

- **Reminder** (deadline tomorrow) → LO, cc Owen (+ CEO per config)
- **Past due** (deadline passed) → LO, cc Processor, cc Owen (+ CEO); repeats
  daily until the loan is resolved
- **Exceptions** (LO deactivated / no valid email) → routed to Owen for
  reassignment; the departed LO is never emailed

LO and processor emails come straight from Vesta (the single Redash query), so
there is no roster to maintain.

## Setup (one time)

1. **Gmail app password** on owen.sheehan@multiplymortgage.com (2FA → App
   passwords → Mail).
2. **Redash API key** for the alert query (query menu → Show API Key).
3. **GitHub secrets** (Settings → Secrets and variables → Actions):
   - `REDASH_API_KEY`
   - `GMAIL_ADDRESS` = owen.sheehan@multiplymortgage.com
   - `GMAIL_APP_PASSWORD`

## Config (env in the workflow)

- `REDASH_QUERY_ID` — the combined alert query id (default 335)
- `CEO_MODE` — `every` (cc CEO on all, default), `none`, or `digest`
  (one summary email to CEO instead of per-loan cc)
- `DRY_RUN` — `true` (default; logs, sends nothing) / `false` (live)

## Go-live order

1. Push, add secrets.
2. Confirm the query returns rows (it does — there's a real past-due backlog).
3. Run the workflow manually with `DRY_RUN=true`; read the log.
4. **Check processor emails** — some are on `@withmultiply.com`, not
   `@multiplymortgage.com`. Confirm those inboxes are real before cc'ing daily.
5. Consider triaging the >200-day-overdue backlog before going live, so day one
   isn't a mass blast about year-old loans.
6. Set `DRY_RUN=false`, push.

## Notes

- Deactivated LOs' emails come tagged `... (deactivated)` in Vesta; the script
  treats those as invalid and routes the loan to Owen instead of emailing them.
- Reads the query's cached result — set the query's Redash refresh to run
  before the Action (e.g. 12:30 UTC; Action at 13:00 UTC).
- Cron is UTC, no daylight-saving adjustment.

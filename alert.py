#!/usr/bin/env python3
"""
TRID / LE deadline alert.

Pulls a single Redash query that returns correspondent loans whose Loan
Estimate deadline is approaching (due tomorrow) or already past, and whose
LE-sent field is still blank. Sends:

  * REMINDER  (deadline tomorrow)  -> Loan Officer, cc Owen (+ CEO per config)
  * PAST_DUE  (deadline passed)    -> Loan Officer, cc Processor, cc Owen (+ CEO)

Loans whose LO is deactivated, has no valid email, or is missing are NOT
emailed to that LO. They are routed to Owen as exceptions so a live person
can reassign — nothing falls through because an assignee left.

The Redash query supplies a `routing` column:
    send_to_lo         -> active LO with a valid email
    exception_to_owner -> deactivated / blank status / missing email

Safety: DRY_RUN defaults to true — logs what WOULD send, sends nothing.
Set DRY_RUN=false in the workflow only after verifying a run.
"""

import os
import time
import smtplib
import requests
from email.mime.text import MIMEText
from collections import defaultdict

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
REDASH_BASE_URL   = os.environ.get("REDASH_BASE_URL", "https://redash.withmultiply.com")
REDASH_QUERY_ID   = os.environ.get("REDASH_QUERY_ID", "335")
REDASH_API_KEY    = os.environ.get("REDASH_API_KEY", "")

GMAIL_ADDRESS     = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

CEO_EMAIL         = os.environ.get("CEO_EMAIL", "michael@multiplymortgage.com")
ADMIN_EMAIL       = os.environ.get("ADMIN_EMAIL", "owen.sheehan@multiplymortgage.com")

# How the CEO is included: "every" (cc on all), "none" (never cc; still gets
# exceptions if you add that), or "digest" (one summary email, no per-loan cc).
CEO_MODE          = os.environ.get("CEO_MODE", "every").lower()

DRY_RUN           = os.environ.get("DRY_RUN", "true").lower() != "false"

# TEST_MODE: actually send real emails, but redirect EVERY recipient (LO,
# processor, CEO, exceptions) to TEST_EMAIL so only you receive them. Lets you
# verify real delivery and formatting without anyone else getting mail. The
# original intended recipients are shown in a banner at the top of each email.
TEST_MODE         = os.environ.get("TEST_MODE", "false").lower() == "true"
TEST_EMAIL        = os.environ.get("TEST_EMAIL", "owen.sheehan@multiplymortgage.com")

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def valid_email(addr):
    """Reject blank and Vesta's '(deactivated)'-tagged addresses."""
    if not addr:
        return False
    a = addr.strip().lower()
    if not a or "(deactivated)" in a or " " in a:
        return False
    return "@" in a


# ---------------------------------------------------------------------------
# Redash
# ---------------------------------------------------------------------------
def fetch_rows():
    if not REDASH_API_KEY:
        raise SystemExit("ERROR: REDASH_API_KEY is not set.")
    url = f"{REDASH_BASE_URL}/api/queries/{REDASH_QUERY_ID}/results.json"
    resp = requests.get(url, headers={"Authorization": f"Key {REDASH_API_KEY}"}, timeout=60)
    resp.raise_for_status()
    return resp.json()["query_result"]["data"]["rows"]


# ---------------------------------------------------------------------------
# Email bodies
# ---------------------------------------------------------------------------
def loan_line(ln):
    due = ln.get("le_due")
    days = ln.get("days_to_due")
    if ln.get("alert_type") == "past_due":
        overdue = f"{days} day(s) PAST DUE" if days is not None else "PAST DUE"
        timing = f"LE was due {due} — {overdue}"
    else:
        timing = f"LE due TOMORROW ({due})"
    return (f"  - Loan {ln['loannumber']} — {ln['borrower']} "
            f"(stage: {ln['currentloanstage']}; {timing})")


def lo_body(lo_name, loans):
    first = lo_name.split()[0] if lo_name else "there"
    has_pastdue = any(l.get("alert_type") == "past_due" for l in loans)
    lead = ("One or more of your loans are PAST their Loan "
            "Estimate deadline with no LE sent date on file:"
            if has_pastdue else
            "You have a loan with a Loan Estimate deadline "
            "tomorrow and no LE sent date on file:")
    lines = [f"Hi {first},", "", lead, ""]
    lines += [loan_line(l) for l in sorted(loans, key=lambda x: x['loannumber'])]
    lines += [
        "",
        "Please take one of these actions:",
        "  1. If the LE has not been sent, send it now.",
        "  2. If it was already sent, update the loan so Vesta captures the "
        "sent date.",
        "",
        "A blank LE-sent field is what puts a loan on this list. Loans past "
        "their deadline will keep appearing daily until resolved.",
        "",
        "Compliance Monitoring (automated)",
    ]
    return "\n".join(lines)


def exception_body(loans):
    lines = [
        "The TRID alert found loan(s) whose loan officer is deactivated or "
        "has no valid email, so no LO could be notified. These need a live "
        "owner — please reassign or action:",
        "",
    ]
    for l in sorted(loans, key=lambda x: x['loannumber']):
        lines.append(
            f"  - Loan {l['loannumber']} — {l['borrower']} "
            f"(stage: {l['currentloanstage']}; LO on file: "
            f"\"{l['loan_officer']}\", status: {l.get('lo_status')}; "
            f"LE due: {l.get('le_due')})"
        )
    lines += ["", "Compliance Monitoring (automated)"]
    return "\n".join(lines)


def ceo_digest_body(by_lo, exceptions):
    total = sum(len(v) for v in by_lo.values()) + len(exceptions)
    lines = [f"TRID LE alert summary — {total} open loan(s) flagged today.", ""]
    for lo, loans in sorted(by_lo.items()):
        pd = sum(1 for l in loans if l.get("alert_type") == "past_due")
        lines.append(f"  {lo}: {len(loans)} loan(s) ({pd} past due)")
    if exceptions:
        lines += ["", f"Exceptions (deactivated/missing LO): {len(exceptions)} "
                      "loan(s) routed to Owen for reassignment."]
    lines += ["", "Compliance Monitoring (automated)"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------
def send(smtp, to_addr, cc, subject, body):
    cc = [c for c in cc if valid_email(c)]
    if TEST_MODE:
        # Redirect everything to the tester; show the real recipients up top.
        banner = (
            "*** TEST MODE ***\n"
            f"This would have been sent TO: {to_addr}\n"
            f"CC: {', '.join(cc) if cc else '(none)'}\n"
            "All recipients redirected to you for testing.\n"
            + ("-" * 50) + "\n\n"
        )
        body = banner + body
        subject = "[TEST] " + subject
        real_to = TEST_EMAIL
        real_cc = []
    else:
        real_to = to_addr
        real_cc = cc

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = real_to
    if real_cc:
        msg["Cc"] = ", ".join(real_cc)
    smtp.sendmail(GMAIL_ADDRESS, [real_to] + real_cc, msg.as_string())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    rows = fetch_rows()
    print(f"Query {REDASH_QUERY_ID} returned {len(rows)} row(s).")
    if not rows:
        print("Nothing flagged. Done.")
        return

    # Dedupe by loannumber (a loan could repeat if >1 processor); keep first,
    # collect processor emails across duplicates.
    loans = {}
    proc_emails = defaultdict(set)
    for r in rows:
        ln = r["loannumber"]
        if ln not in loans:
            loans[ln] = r
        if valid_email(r.get("processor_email")):
            proc_emails[ln].add(r["processor_email"].strip())

    # Split by routing
    to_lo = defaultdict(list)      # lo_email -> [loans]
    lo_name_by_email = {}
    exceptions = []
    for ln, r in loans.items():
        if r.get("routing") == "send_to_lo" and valid_email(r.get("loan_officer_email")):
            em = r["loan_officer_email"].strip()
            to_lo[em].append(r)
            lo_name_by_email[em] = r.get("loan_officer", "")
        else:
            exceptions.append(r)

    print(f"LOs to notify: {len(to_lo)}. Exception loans: {len(exceptions)}.")

    # CEO cc per mode
    per_email_ceo_cc = [CEO_EMAIL] if CEO_MODE == "every" else []

    def describe():
        for em, ls in to_lo.items():
            cc = [ADMIN_EMAIL] + per_email_ceo_cc
            # add processor emails for any past-due loan in this batch
            pcs = set()
            for l in ls:
                pcs |= proc_emails.get(l["loannumber"], set())
            cc += sorted(pcs)
            n_pd = sum(1 for l in ls if l.get("alert_type") == "past_due")
            subj = (f"[TRID] {len(ls)} loan(s) need LE action"
                    + (f" — {n_pd} PAST DUE" if n_pd else " — due tomorrow"))
            yield ("lo", em, cc, subj, lo_body(lo_name_by_email[em], ls))
        if exceptions:
            yield ("exc", ADMIN_EMAIL, per_email_ceo_cc,
                   f"[TRID] {len(exceptions)} loan(s) with deactivated/missing LO",
                   exception_body(exceptions))

    if DRY_RUN:
        print("\n=== DRY RUN — nothing sent ==="
              + ("  (DRY_RUN overrides TEST_MODE)" if TEST_MODE else ""))
        for _, to, cc, subj, body in describe():
            print(f"\nTO: {to}\nCC: {', '.join(cc) if cc else '(none)'}\nSUBJECT: {subj}\n{body}")
        if CEO_MODE == "digest":
            print(f"\nTO: {CEO_EMAIL}  (digest)\n{ceo_digest_body(dict((lo_name_by_email[e], l) for e,l in to_lo.items()), exceptions)}")
        print("\n=== END DRY RUN ===")
        return

    if TEST_MODE:
        print(f"\n*** TEST MODE — all emails redirected to {TEST_EMAIL} ***")

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.starttls()
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        for _, to, cc, subj, body in describe():
            send(smtp, to, cc, subj, body)
            print(f"Sent: {to} (cc {len(cc)})")
            time.sleep(1)
        if CEO_MODE == "digest":
            send(smtp, CEO_EMAIL, [],
                 f"[TRID] Daily LE alert summary",
                 ceo_digest_body(dict((lo_name_by_email[e], l) for e,l in to_lo.items()), exceptions))
            print(f"Sent digest to {CEO_EMAIL}")
    print("Done.")


if __name__ == "__main__":
    main()

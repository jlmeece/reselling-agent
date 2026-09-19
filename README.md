# Reselling Tool

Automated Costco → eBay arbitrage. The tool finds Costco products worth reselling, researches and scores them, and watches your active listings — running itself on a schedule so you only touch it a few minutes a day.

> This is a **user guide** for operating the tool and understanding its features. For build details (modes, column map, file layout), see `CLAUDE.md`.

---

## The mental model (one paragraph)

Products move through stages automatically. The tool discovers them, researches them, prices them, and keeps watching them once they're listed. You make **two kinds of decisions**, nothing more:

1. **Approve or reject** a product the tool has researched.
2. **List or cut** a product, and handle the actual eBay listing/reprice yourself.

Everything else — pricing math, comps, stock checks, margin tracking, cleanup — is the tool's job.

---

## The product lifecycle

```
discovery ──► PENDING ──► SCORED ──► APPROVED ──► READY ──► ACTIVE
                                                            │
                 (underperformers and junk) ◄── rotation + audit
```

| Stage | What it means | Your job |
|-------|--------------|----------|
| **PENDING** | Found, not yet researched | Nothing |
| **SCORED** | Researched and priced | **Approve or pause it** |
| **APPROVED** | You approved it | Nothing (next daily sweep promotes it) |
| **READY** | Ready to list | **Export CSV and list on eBay** |
| **ACTIVE** | Live on eBay | Nothing (tool watches stock/price) |
| **PAUSED** | Held back (out of stock, or margin too thin) | Nothing, or cut it |
| **AUDIT_REVIEW** | Flagged by the audit for a decision | **Review and decide** |

---

## The three places you look

The tool used to scatter you across a terminal menu, a sheet, and a half-built dashboard. Now there's a clear split — and you rarely need the sheet.

**1. Telegram (your phone) — the daily surface.**
Alerts land here: crashes, cookie warnings, price changes, sale countdowns, the audit digest. Plus the `/lookup` command to check any product in seconds.

**2. Dashboard (desktop, localhost) — the weekly surface.**
The big picture: portfolio health, top opportunities ranked by Sharpe, active listings, margins per category. Open it when you're sitting down to review, not every day.

**3. Google Sheets — the source of truth (rarely touch).**
Everything is stored here. You almost never need to open it now — the dashboard mirrors it honestly, and Telegram surfaces the important events. Open it only when you want to see the raw record.

---

## Features

**Research and scoring.** Each product is researched — eBay comps, spot price (for metals), demand signals — and scored against its category's rules.

**Real margins.** The tool calculates net margin using the correct eBay fee for each category (bullion is ~4%, jewelry ~15%, etc.), so the number you see is what you'd actually keep, not a generic guess.

**MPT / Sharpe ranking.** The tool scores the whole portfolio with a Sharpe ratio — return vs. risk — and ranks products. "Top Opportunities" shows you where to point your capital first. This is real portfolio math, not a vibes-based "best guess."

**Rotation (weekly).** Every Sunday the tool scores all active products and flags underperformers so you can cut dead weight and redeploy the money.

**Audit and graveyard (every 2 days).** Automatically removes junk rows and flags borderline ones for your review, so the sheet doesn't fill up with noise.

**Alerts that actually tell you what's happening.** Crash alerts, cookie-expiry warnings, price-change alerts (old → new + margin change), sale-countdown alerts, and the audit digest. No more silent failures.

**Dead-man switch (healthchecks).** If a scheduled run doesn't complete on time, you get pinged — so a silent failure can't go unnoticed for weeks again.

---

## The schedule (runs itself)

Windows Task Scheduler fires these automatically. You do nothing to trigger them.

| Time | Job |
|------|-----|
| 8:00 AM | Audit (every 2 days) |
| 9:00 AM | Research (daily) |
| 9:30 AM | Active check |
| 10:00 AM | Daily sweep |
| 1:00 PM | Active check |
| 6:00 PM | Active check |
| 9:00 PM | Active check |
| Sunday 9:00 AM | Rotation (weekly digest) |

Discovery (finding new products) is run on demand — it needs a live Costco browser session.

---

## Your routine

**Daily (a few minutes, from your phone):**
Check Telegram. If anything pinged you, act on it. If a product hit SCORED, approve or pause it. That's usually it.

**When an eBay order lands:**
Send `/lookup <product name>` to the bot and confirm the details before you ship. Takes seconds.

**Weekly (Sunday, at your desk):**
Open the dashboard. Read the rotation digest. Review Top Opportunities. Export any READY products and list them on eBay.

**Roughly monthly (~every 25 days):**
Refresh your Costco cookies (see below). This is the one recurring manual chore the tool can't do for you — it requires a real browser login.

---

## Alerts — what each one means

- **"Scheduler crashed"** — a run errored out. The tool will retry, but glance at it so a real problem doesn't hide.
- **Cookie warning** — your Costco cookies are aging or expiring. Refresh them soon, or research/active checks will start failing.
- **Price change** — a watched product's price moved. Shows old → new and the new margin, so you can decide whether to reprice.
- **Sale countdown** — a sale is about to expire (48h warn, 24h urgent). Time to reprice or plan the listing end.
- **Audit digest** — what was removed and what's flagged for your review.

---

## The manual pieces (kept manual on purpose)

**Costco cookies.** Costco blocks automated logins, so the tool reuses your real Chrome session cookies. When they expire (roughly every 25 days), you refresh them once:

1. Open VS Code, terminal, `cd "C:\Users\jorda\projects\reselling-agent"`
2. `.\run.ps1 cookies` — log into Costco in Chrome, export cookies
3. `python tools/cookie_sync.py upload` — sync them

**eBay actions.** Listing, repricing, and delisting are yours. The tool flags and recommends — it never auto-acts on your eBay account. You're the safety net.

---

## `/lookup` — ask your phone about any product

Send the bot `/lookup` with a product name or category. You get back a tight card: Costco → eBay price, net margin, stock, sale countdown, and a "last checked Xh ago" stamp so you know if the data is stale.

---

## Status glossary

- **PENDING** — found, awaiting research
- **SCORED** — researched, awaiting your approve/pause decision
- **APPROVED** — you approved it; next daily sweep promotes it
- **READY** — export the CSV and list on eBay
- **ACTIVE** — live, being monitored
- **PAUSED_OOS** — out of stock at Costco
- **PAUSED_MARGIN** — margin below your floor
- **CHECK FAILED** — Costco scrape blocked; needs a recheck
- **AUDIT_REVIEW** — flagged for your decision

---

## For builders

This README is the user-facing layer. For how the code is organized, the 8 run modes, the sheet column map, and build conventions, see `CLAUDE.md`.

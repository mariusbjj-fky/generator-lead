# Lead generator — Romanian clinics (dental · aesthetic · optics · medical)

Research-only pipeline that finds and verifies B2B leads from public sources and writes one Excel workbook.
It never contacts anyone.

| Category | What | Sources |
|---|---|---|
| 1 New businesses | registered in the last 7 days, target CAEN + keyword rules | ONRC open data (data.gov.ro) → verified by ANAF API |
| 2A / 2B Inactive | last post on all found platforms > 60 days; 2B = still running Meta ads | Instagram / Facebook / TikTok + Meta Ad Library (Apify) |
| 3 No website | none (verified), broken, parked, expired SSL, social-only, weak presence | direct HTTP check, Google Business Profile (Apify), web search |

## Quick start
```powershell
python run.py all                 # free sources only (ONRC, ANAF, website checks)
python run.py all --paid --area "București" --area "Cluj-Napoca"   # + Google Maps, social, ads (Apify, capped)
python run.py export              # workbook in data\output\
```
In Claude Code, just ask *"run lead research"* (or `/lead-research`). Claude runs the scripts and also does the
web-search steps (finding websites and social profiles with evidence) that scripts must not guess.

Requirements: Python 3.11+, `openpyxl`. Paid steps need `APIFY_TOKEN` (`setx APIFY_TOKEN "<token>"`).
Spend caps: `config.json` → `limits` (default 1 USD per run, 5 USD per week). A typical paid test run cost ≈ 0.25 USD.

## Output (`data/output/leads_<stamp>.xlsx`)
Summary · Cat 1 - New Businesses · Cat 2A - Inactive No Ads · Cat 2B - Inactive With Ads · Cat 3 - No Website ·
All Leads (dedup by CUI) · Needs Review · Run Log. Every tab is sorted by score. A CSV of the merged list is written alongside.
Excel caps sheet names at 31 characters, hence "All Leads (dedup by CUI)".

## Viewer (web/)
A dependency-free page to browse the results: category tiles, charts by industry/score/city, search and filters,
a detail view per lead (all fields + source links), Summary and Run Log tabs, and CSV download of the filtered list.

- Local: `python run.py export` (or `python run.py web`) writes `web/data/leads.json`, then
  `python -m http.server 8000 -d web` and open http://localhost:8000.
- Vercel: import this repo; `vercel.json` serves `web/` as a static site (no build). Lead data is **not** in the
  repo (`data/` and `web/data/` are git-ignored), so the deployed page starts empty: drag `web/data/leads.json`
  onto it — the file is read in your browser and never uploaded. To publish data with the site, remove
  `web/data/` from `.gitignore` and commit `leads.json` — only do this if the repo and the Vercel site are private.

## Known source limits (logged in Run Log every run)
- myportal.onrc.ro needs a login → not used. Monitorul Oficial Part IV needs a subscription → not used.
- listafirme.ro is behind a Cloudflare challenge → not bypassed; a manual-check link is stored per lead.
  ANAF's official API is the second verification source instead.
- The ONRC open-data snapshot is published every few weeks; registrations after the snapshot date are not covered (logged).
- Ad Library counts come from the page's active ads (all countries).

## Files
`run.py` CLI · `leadgen/` modules (`onrc`, `verify`, `maps`, `websites`, `social`, `ads`, `classify`, `export`, `core`) ·
`config.json` industries, CAEN codes (Rev 2 + Rev 3), keywords, scoring, caps · `data/leads.csv` register (backed up on save) ·
`data/run_log.csv` · `data/costs.csv` · `data/runs/` raw Apify output · `data/cache/` ONRC extracts.

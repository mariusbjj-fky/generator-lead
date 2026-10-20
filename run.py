"""Lead research pipeline — command line.

  python run.py all [--paid] [--area București] [--industry dental]   full run (see README)
  python run.py new [--days 7]            Cat 1: newly registered companies (ONRC open data)
  python run.py verify [--lead L-00001]   cross-check CUIs with ANAF (+ listafirme probe)
  python run.py maps --industry dental --area "Cluj-Napoca" [--max 40] [--usd 1] [--website without]
  python run.py gbp [--lead ...]          find Google Business Profiles for register leads (Apify)
  python run.py websites [--force]        check websites (free)
  python run.py social [--network instagram] [--usd 0.5]   last post dates (Apify)
  python run.py ads [--only-inactive]     Meta Ad Library active-ads check (Apify)
  python run.py score                     categories + priority score
  python run.py export                    Excel workbook + CSV + summary (also refreshes the web viewer data)
  python run.py web                       only rebuild web/data/leads.json for the viewer
  python run.py pending --task website|social|review     work list for the web-search step
  python run.py set L-00001 facebook_url=https://… --evidence "same phone in page About"
  python run.py add leads.json            add leads found by web search
  python run.py show L-00001 | list [--cat 2B] | log | session
Research only: nothing here sends messages or contacts anyone.
"""
import argparse
import json
import sys

from leadgen import core as C


def cmd_set(a):
    rows = C.load()
    r = next((x for x in rows if x["lead_id"] == a.lead), None)
    if r is None:
        sys.exit(f"Unknown lead {a.lead}")
    upd = {}
    for kv in a.pairs:
        if "=" not in kv:
            sys.exit(f"Expected field=value, got '{kv}'")
        k, v = kv.split("=", 1)
        if k not in C.COLUMNS:
            sys.exit(f"Unknown field '{k}'")
        if k in C.ALLOWED and v not in C.ALLOWED[k]:
            sys.exit(f"{k} must be one of {C.ALLOWED[k]}")
        upd[k] = v
    social = [k for k in upd if k in ("facebook_url", "instagram_url", "tiktok_url") and upd[k]]
    if social and not a.evidence:
        sys.exit("Linking a social profile needs --evidence (what matches: name + city, phone, website link…).")
    if upd.get("website_status") == "NONE" and not (a.evidence or upd.get("website_search_note")):
        sys.exit("website_status=NONE needs --evidence with the searches done (e.g. 'Google: \"<name> <city>\", "
                 "Maps, FB/IG bio — no site, 2026-10-20').")
    for k in social:
        u, _ = C.social_handle(upd[k], k.split("_")[0])
        if not u:
            sys.exit(f"{upd[k]} is not a profile URL for {k}")
        upd[k] = u
        upd.setdefault(k.replace("_url", "_status"), "FOUND")
    if a.evidence:
        if upd.get("website_status") == "NONE":
            upd["website_search_note"] = a.evidence
            upd["website_checked_at"] = C.now_iso()
        else:
            upd["identity_evidence"] = a.evidence
    if a.source:
        upd["sources"] = " | ".join(f"{k}: {a.source}" for k in upd if k not in ("identity_evidence", "sources"))
    C.merge(r, upd)
    C.save(rows)
    print(f"{a.lead} updated: {', '.join(upd)}")


def cmd_add(a):
    with open(a.file, encoding="utf-8") as f:
        items = json.load(f)
    rows = C.load()
    n_new = 0
    for it in items:
        bad = [k for k in it if k not in C.COLUMNS]
        if bad:
            sys.exit(f"Unknown fields {bad}")
        if any(it.get(k) for k in ("facebook_url", "instagram_url", "tiktok_url")) and not it.get("identity_evidence"):
            sys.exit(f"{it.get('brand_name') or it.get('legal_name')}: social link without identity_evidence")
        it.setdefault("origin", "web_search")
        _, new = C.upsert(rows, it)
        n_new += new
    C.save(rows)
    C.log("add", "web search (Claude)", query=a.file, results=len(items), note=f"{n_new} new")


def cmd_pending(a):
    rows = [r for r in C.load() if r["verification_status"] != "DISCARDED"
            and not ("cat1" in r["origin"] and r["verification_status"] != "VERIFIED")]
    if a.task == "website":
        sel = [r for r in rows if r["website_status"] in ("NOT_FOUND_IN_SOURCES", "UNKNOWN", "")]
    elif a.task == "social":
        sel = [r for r in rows if not r["social_search_note"]
               and not all(r[c] for _, c in C.SOCIAL)]
    else:
        from leadgen import export
        sel = export.needs_review(C.load())
    for r in sel:
        print(json.dumps({k: r[k] for k in ("lead_id", "legal_name", "brand_name", "industry", "city", "county",
                                             "cui", "phone", "website_url", "website_status", "facebook_url",
                                             "instagram_url", "tiktok_url", "gbp_url")}, ensure_ascii=False))
    print(f"# {len(sel)} leads", file=sys.stderr)


def cmd_all(a):
    from leadgen import ads, classify, export, maps, onrc, social, verify, websites
    C.new_session()
    rid = C.new_run_id("all")
    onrc.discover(days=a.days, run_id=rid)
    verify.verify(run_id=rid)
    if a.paid:
        for ind in (a.industry or C.cfg()["industry_priority"]):
            for area in (a.area or C.cfg()["maps"]["areas"]):
                maps.discover(ind, area, max_places=a.max, run_id=rid)
        maps.lookup(run_id=rid)
    else:
        C.log("all", "paid sources (Apify)", note="skipped: run with --paid for Google Maps, social dates, Meta ads",
              run_id=rid)
    websites.run(run_id=rid)
    if a.paid:
        social.run(run_id=rid)
        classify.run()
        ads.run(run_id=rid)
    classify.run()
    export.export()
    print("\nNext: web-search step for `python run.py pending --task website` and `--task social`, "
          "then `python run.py social`, `ads`, `score`, `export`.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("all")
    p.add_argument("--paid", action="store_true", help="include Apify steps (Maps, social, ads)")
    p.add_argument("--area", action="append")
    p.add_argument("--industry", action="append", choices=list(C.cfg()["industries"]))
    p.add_argument("--max", type=int)
    p.add_argument("--days", type=int)
    p = sub.add_parser("new")
    p.add_argument("--days", type=int)
    p = sub.add_parser("verify")
    p.add_argument("--lead", action="append")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("maps")
    p.add_argument("--industry", required=True, choices=list(C.cfg()["industries"]))
    p.add_argument("--area", required=True)
    p.add_argument("--max", type=int)
    p.add_argument("--usd", type=float)
    p.add_argument("--website", choices=["all", "without", "with"], default="all")
    p = sub.add_parser("gbp")
    p.add_argument("--lead", action="append")
    p.add_argument("--usd", type=float)
    p.add_argument("--limit", type=int, default=40)
    p = sub.add_parser("websites")
    p.add_argument("--lead", action="append")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("social")
    p.add_argument("--network", action="append", choices=["instagram", "facebook", "tiktok"])
    p.add_argument("--lead", action="append")
    p.add_argument("--usd", type=float)
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("ads")
    p.add_argument("--lead", action="append")
    p.add_argument("--usd", type=float)
    p.add_argument("--force", action="store_true")
    p.add_argument("--only-inactive", action="store_true")
    sub.add_parser("score")
    p = sub.add_parser("export")
    p.add_argument("--out")
    sub.add_parser("web")
    p = sub.add_parser("pending")
    p.add_argument("--task", choices=["website", "social", "review"], default="review")
    p = sub.add_parser("set")
    p.add_argument("lead")
    p.add_argument("pairs", nargs="+")
    p.add_argument("--evidence")
    p.add_argument("--source", help="URL that verifies the value(s)")
    p = sub.add_parser("add")
    p.add_argument("file")
    p = sub.add_parser("show")
    p.add_argument("lead")
    p = sub.add_parser("list")
    p.add_argument("--cat")
    sub.add_parser("log")
    sub.add_parser("session")
    a = ap.parse_args()

    if a.cmd == "all":
        cmd_all(a)
    elif a.cmd == "new":
        from leadgen import onrc
        print(onrc.discover(days=a.days))
    elif a.cmd == "verify":
        from leadgen import verify
        verify.verify(lead_ids=a.lead, force=a.force)
    elif a.cmd == "maps":
        from leadgen import maps
        print(maps.discover(a.industry, a.area, max_places=a.max, usd=a.usd, website=a.website))
    elif a.cmd == "gbp":
        from leadgen import maps
        print(maps.lookup(lead_ids=a.lead, usd=a.usd, limit=a.limit))
    elif a.cmd == "websites":
        from leadgen import websites
        websites.run(lead_ids=a.lead, force=a.force)
    elif a.cmd == "social":
        from leadgen import social
        social.run(networks=a.network or ("instagram", "facebook", "tiktok"), lead_ids=a.lead, usd=a.usd, force=a.force)
    elif a.cmd == "ads":
        from leadgen import ads
        print(ads.run(lead_ids=a.lead, usd=a.usd, force=a.force, only_inactive=a.only_inactive))
    elif a.cmd == "score":
        from leadgen import classify
        classify.run()
    elif a.cmd == "export":
        from leadgen import classify, export, web
        classify.run()
        export.export(a.out)
        web.build()
    elif a.cmd == "web":
        from leadgen import web
        web.build()
    elif a.cmd == "pending":
        cmd_pending(a)
    elif a.cmd == "set":
        cmd_set(a)
    elif a.cmd == "add":
        cmd_add(a)
    elif a.cmd == "show":
        r = next((x for x in C.load() if x["lead_id"] == a.lead), None)
        print(json.dumps({k: v for k, v in (r or {}).items() if v}, ensure_ascii=False, indent=1))
    elif a.cmd == "list":
        for r in C.load():
            if a.cat and a.cat not in [x.strip() for x in r["categories"].split(",")]:
                continue
            print(f"{r['lead_id']}  {r['score'] or '-':>3} {r['priority'] or '':6} {r['categories'] or '-':10} "
                  f"{(r['brand_name'] or r['legal_name'])[:40]:40} {r['city'][:22]:22} {r['verification_status']}")
    elif a.cmd == "log":
        for l in C.load_log(C.session_id()):
            print(" | ".join(l[k] for k in ("timestamp", "step", "source", "query", "results", "errors", "blocked", "note")))
    elif a.cmd == "session":
        print("New session:", C.new_session())


if __name__ == "__main__":
    main()

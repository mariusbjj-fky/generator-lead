"""Category 2 input: date of the most recent post on Instagram / Facebook / TikTok (Apify, capped).

Only profiles already linked to a lead are checked (official link on the website/GBP, or linked by
the web-search step with identity evidence). Pinned posts are ignored. If no date can be read
(private, restricted, empty response) the date stays blank and the status says why.
"""
from . import core as C


def _date(ts):
    d = C.parse_date(ts) if ts else None
    if d is None and isinstance(ts, (int, float)) and ts > 0:
        from datetime import datetime
        d = datetime.fromtimestamp(ts, C.tz()).date()
    return d.isoformat() if d else ""


def _pick(rows, col, chk, lead_ids, force):
    return [r for r in rows if r[col] and r["verification_status"] != "DISCARDED"
            and (not lead_ids or r["lead_id"] in lead_ids) and (force or lead_ids or C.is_stale(r[chk]))]


def instagram(rows, usd, run_id):
    by = {}
    for r in rows:
        _, h = C.social_handle(r["instagram_url"], "instagram")
        if h:
            by.setdefault(h, []).append(r)
    if not by:
        return
    try:
        items, spent, status = C.apify_run(C.cfg()["apify"]["instagram_actor"], {"usernames": list(by)}, usd,
                                           run_id, "instagram")
    except Exception as e:
        C.log("social", "Instagram (Apify)", query=f"{len(by)} profiles", errors=str(e), run_id=run_id)
        return
    seen = set()
    for it in items:
        h = (it.get("username") or "").lower()
        if h not in by:
            continue
        seen.add(h)
        posts = [p for p in (it.get("latestPosts") or []) if not p.get("isPinned")]
        last = max((_date(p.get("timestamp")) for p in posts if p.get("timestamp")), default="")
        upd = {"instagram_checked_at": C.now_iso(), "instagram_last_post": last, "instagram_status": "FOUND",
               "sources": f"instagram_last_post: {by[h][0]['instagram_url']} (Apify {C.today()})"}
        if it.get("private"):
            upd.update({"instagram_status": "PRIVATE", "instagram_last_post": ""})
        elif it.get("postsCount") == 0:
            upd.update({"instagram_last_post": "NO POSTS"})
        elif not last:
            upd.update({"instagram_status": "ERROR", "notes": "IG: no post dates returned — check manually"})
        bio_links = " ".join([it.get("externalUrl") or ""] + [x.get("url", "") if isinstance(x, dict) else str(x)
                                                             for x in (it.get("externalUrls") or [])]).lower()
        for r in by[h]:
            u = dict(upd)
            dom = C.domain_of(r["website_url"])
            if dom and dom in bio_links:
                u["identity_evidence"] = f"instagram bio links to {dom}"
            if not r["website_url"] and bio_links.strip():
                u["notes"] = f"IG bio link: {bio_links.strip()[:120]}"
            C.merge(r, u)
    for h, rs in by.items():
        if h not in seen:
            for r in rs:
                C.merge(r, {"instagram_checked_at": C.now_iso(), "instagram_status": "NOT_FOUND",
                            "notes": "IG: profile not returned (renamed/deleted/blocked) — check manually"})
    C.log("social", "Instagram (Apify)", query=f"{len(by)} profiles", results=len(seen), run_id=run_id,
          errors="" if status == "SUCCEEDED" else f"actor status {status}", note=f"cost {spent:.3f} USD")


def facebook(rows, usd, run_id):
    by = {}
    for r in rows:
        u, _ = C.social_handle(r["facebook_url"], "facebook")
        if u:
            by.setdefault(u, []).append(r)
    if not by:
        return
    try:
        items, spent, status = C.apify_run(C.cfg()["apify"]["facebook_posts_actor"],
                                           {"startUrls": [{"url": u} for u in by], "resultsLimit": 3},
                                           usd, run_id, "facebook")
    except Exception as e:
        C.log("social", "Facebook (Apify)", query=f"{len(by)} pages", errors=str(e), run_id=run_id)
        return
    last = {}
    for it in items:
        u, _ = C.social_handle(it.get("facebookUrl") or it.get("inputUrl") or it.get("pageUrl") or "", "facebook")
        if not u:
            continue
        d = _date(it.get("time") or it.get("timestamp"))
        if d and d > last.get(u, ""):
            last[u] = d  # max() also neutralises an old pinned post at the top
    for u, rs in by.items():
        upd = {"facebook_checked_at": C.now_iso(), "facebook_last_post": last.get(u, ""),
               "facebook_status": "FOUND" if u in last else "ERROR",
               "sources": f"facebook_last_post: {u} (Apify {C.today()})"}
        if u not in last:
            upd["notes"] = "FB: no public post read (private, restricted, empty or blocked) — check manually"
        for r in rs:
            C.merge(r, upd)
    C.log("social", "Facebook (Apify)", query=f"{len(by)} pages", results=len(last), run_id=run_id,
          errors="" if status == "SUCCEEDED" else f"actor status {status}", note=f"cost {spent:.3f} USD")


def tiktok(rows, usd, run_id):
    by = {}
    for r in rows:
        _, h = C.social_handle(r["tiktok_url"], "tiktok")
        if h:
            by.setdefault(h, []).append(r)
    if not by:
        return
    inp = {"profiles": list(by), "resultsPerPage": 5, "excludePinnedPosts": True, "profileSorting": "latest",
           "shouldDownloadVideos": False, "shouldDownloadCovers": False}
    try:
        items, spent, status = C.apify_run(C.cfg()["apify"]["tiktok_actor"], inp, usd, run_id, "tiktok")
    except Exception as e:
        C.log("social", "TikTok (Apify)", query=f"{len(by)} profiles", errors=str(e), run_id=run_id)
        return
    last, seen = {}, set()
    for it in items:
        h = ((it.get("authorMeta") or {}).get("name") or it.get("input") or "").lower().lstrip("@")
        if h not in by:
            continue
        seen.add(h)
        if it.get("isPinned"):
            continue
        d = _date(it.get("createTimeISO") or it.get("createTime"))
        if d and d > last.get(h, ""):
            last[h] = d
    for h, rs in by.items():
        upd = {"tiktok_checked_at": C.now_iso(), "tiktok_last_post": last.get(h, ""),
               "tiktok_status": "FOUND" if h in last else ("ERROR" if h in seen else "NOT_FOUND"),
               "sources": f"tiktok_last_post: https://www.tiktok.com/@{h} (Apify {C.today()})"}
        if h not in last:
            upd["notes"] = "TikTok: no video dates returned (private, empty or blocked) — check manually"
        for r in rs:
            C.merge(r, upd)
    C.log("social", "TikTok (Apify)", query=f"{len(by)} profiles", results=len(last), run_id=run_id,
          errors="" if status == "SUCCEEDED" else f"actor status {status}", note=f"cost {spent:.3f} USD")


def run(networks=("instagram", "facebook", "tiktok"), lead_ids=None, usd=None, force=False, limit=100, run_id=None):
    run_id = run_id or C.new_run_id("social")
    usd = usd or min(0.5, C.cfg()["limits"]["max_usd_per_run"])
    rows = C.load()
    for net, fn in (("instagram", instagram), ("facebook", facebook), ("tiktok", tiktok)):
        if net in networks:
            sel = _pick(rows, f"{net}_url", f"{net}_checked_at", lead_ids, force)[:limit]
            print(f"{net}: {len(sel)} profiles to check")
            if sel:
                fn(sel, usd, run_id)
                C.save(rows)

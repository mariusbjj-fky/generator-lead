"""Meta Ad Library check: is the business running active ads in Romania?

Uses the Apify Facebook Ads Library scraper with `onlyTotal` (one cheap result per page) on the
lead's Facebook page URL, filtered to active ads; `totalCount` is the number of active ads.
The actor counts the page's active ads in all countries (for a Romanian clinic, in practice RO).
Leads without a confirmed Facebook page stay NOT CHECKED, with a keyword Ad Library link for a
manual look. Every lead gets the Ad Library URL used (page-ID link once the page is resolved).
"""
from . import core as C


def _count(item):
    for k in ("totalCount", "total", "adsCount", "count", "totalAds", "resultsCount"):
        v = item.get(k)
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
            return int(v)
    return None


def run(lead_ids=None, usd=None, force=False, limit=100, only_inactive=False, run_id=None):
    run_id = run_id or C.new_run_id("ads")
    rows = C.load()
    live = [r for r in rows if r["verification_status"] != "DISCARDED" and (not lead_ids or r["lead_id"] in lead_ids)]
    for r in live:
        if not r["ad_library_url"]:
            C.merge(r, {"ad_library_url": C.ad_library_url(name=r["brand_name"] or C.norm_name(r["legal_name"]))})
    todo = [r for r in live if r["facebook_url"] and (force or lead_ids or C.is_stale(r["ads_checked_at"]))
            and (not only_inactive or r["activity_status"] == "INACTIVE")][:limit]
    by = {}
    for r in todo:
        u, _ = C.social_handle(r["facebook_url"], "facebook")
        if u:
            by.setdefault(u, []).append(r)
    if not by:
        C.save(rows)
        print("No Facebook pages to check for ads.")
        return {}
    inp = {"startUrls": [{"url": u} for u in by], "onlyTotal": True, "activeStatus": "active", "resultsLimit": 1}
    try:
        items, spent, status = C.apify_run(C.cfg()["apify"]["ads_actor"], inp,
                                           usd or min(0.5, C.cfg()["limits"]["max_usd_per_run"]), run_id, "ads")
    except Exception as e:
        C.log("ads", "Meta Ad Library (Apify)", query=f"{len(by)} pages", errors=str(e), run_id=run_id)
        C.save(rows)
        return {}
    res = {}
    for it in items:
        src = it.get("inputUrl") or it.get("url") or it.get("pageUrl") or it.get("facebookUrl") or ""
        u, _ = C.social_handle(src, "facebook")
        if u in by:
            res[u] = it
    stats = {"ACTIVE": 0, "NOT ACTIVE": 0, "NOT CHECKED": 0}
    for u, rs in by.items():
        it = res.get(u)
        n = _count(it) if it else None
        info = (it or {}).get("pageInfo") or {}
        if info.get("xfbAdLibraryIsCaptchaRequired"):
            n = None  # the Ad Library asked for a CAPTCHA: not bypassed, not counted
        upd = {"ads_checked_at": C.now_iso()}
        if n is None:
            upd.update({"meta_ads_status": "NOT CHECKED",
                        "notes": "Ad Library: no usable answer for this page — check the Ad Library link manually"})
        else:
            upd.update({"meta_ads_status": "ACTIVE" if n > 0 else "NOT ACTIVE", "meta_ads_count": n})
            pid = (info.get("page") or {}).get("id") or it.get("pageId") or it.get("pageID")
            if pid:
                upd["ad_library_url"] = C.ad_library_url(page_id=pid)
        for r in rs:
            u2 = dict(upd)
            u2["sources"] = f"meta_ads_status: {u2.get('ad_library_url') or r['ad_library_url']} (Apify {C.today()})"
            C.merge(r, u2)
        stats[upd["meta_ads_status"]] += 1
    C.save(rows)
    C.log("ads", "Meta Ad Library (Apify)", query=f"{len(by)} pages, active ads, country RO", results=len(res),
          run_id=run_id, errors="" if status == "SUCCEEDED" else f"actor status {status}",
          note=f"cost {spent:.3f} USD; " + ", ".join(f"{k} {v}" for k, v in stats.items()))
    return stats

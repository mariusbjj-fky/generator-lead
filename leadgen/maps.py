"""Google Maps / Google Business Profile via Apify (paid, capped).

discover(): existing businesses in the target industries for an area (feeds Categories 2 and 3).
lookup():   find the Google Business Profile of leads already in the register (e.g. new companies)
            by "<name> <city>", accepted only when the name matches; otherwise AMBIGUOUS.
"""
import math

from . import core as C


def _matches_industry(item, words):
    cats = " ".join([item.get("categoryName") or ""] + list(item.get("categories") or []))
    hay = C.fold(cats + " " + (item.get("title") or ""))
    return any(C.fold(w) in hay for w in words)


def _industry_from_item(item):
    words = C.cfg()["maps"]["category_words"]
    for key in C.cfg()["industry_priority"]:
        if _matches_industry(item, words[key]):
            return key
    return None


def _gbp_fields(item):
    website = item.get("website") or ""
    upd = {
        "gbp_url": item.get("url", ""), "gbp_status": "FOUND", "gbp_checked_at": C.now_iso(),
        "gbp_reviews": item.get("reviewsCount") if item.get("reviewsCount") is not None else "0",
        "gbp_rating": item.get("totalScore") or "",
        "gbp_website": website or "NONE",
    }
    src = item.get("url", "")
    phone = item.get("phoneUnformatted") or item.get("phone") or ""
    if phone:
        upd.update({"phone": phone, "phone_source": f"Google Business Profile {src}"})
    if website and not C.is_social_domain(C.domain_of(website)):
        upd["website_url"] = website
    elif website:
        upd.update({"website_status": "SOCIAL_ONLY", "website_checked_at": C.now_iso(),
                    "website_note": f"GBP website field points to a social/booking page ({website})"})
        for net, col in C.SOCIAL:
            u, _ = C.social_handle(website, net)
            if u:
                upd[col] = u
                upd["identity_evidence"] = f"GBP lists {net} as website"
    # Social links scraped from the business's own website (official link → identity confirmed).
    for key, net, col in (("facebooks", "facebook", "facebook_url"), ("instagrams", "instagram", "instagram_url"),
                          ("tiktoks", "tiktok", "tiktok_url")):
        for link in item.get(key) or []:
            u, _ = C.social_handle(link, net)
            if u and col not in upd:
                upd[col] = u
                upd["identity_evidence"] = " | ".join(
                    x for x in (upd.get("identity_evidence"), f"website links to {net}") if x)
                break
    emails = [e for e in (item.get("emails") or []) if "@" in e]
    if emails:
        upd.update({"email": emails[0].lower(), "email_source": f"business website {upd.get('website_url', '')}"})
    upd["sources"] = f"gbp/phone/website field: {src}"
    if item.get("permanentlyClosed"):
        upd["notes"] = "Google Maps: permanently closed"
    elif item.get("temporarilyClosed"):
        upd["notes"] = "Google Maps: temporarily closed"
    return upd


def discover(industry, area, max_places=None, usd=None, website="all", run_id=None):
    c = C.cfg()
    run_id = run_id or C.new_run_id(f"maps-{industry}")
    terms = c["maps"]["search_terms"][industry]
    want = min(max_places or c["limits"]["max_maps_places_per_run"], c["limits"]["max_maps_places_per_run"])
    actor_input = {
        "searchStringsArray": terms, "locationQuery": f"{area}, Romania",
        "maxCrawledPlacesPerSearch": max(5, math.ceil(want * 1.3 / len(terms))),
        "language": "ro", "skipClosedPlaces": True, "scrapeContacts": True,
        "website": {"all": "allPlaces", "without": "withoutWebsite", "with": "withWebsite"}[website],
        "maxReviews": 0, "maxImages": 0,
    }
    try:
        items, usd_spent, status = C.apify_run(c["apify"]["maps_actor"], actor_input,
                                               usd or c["limits"]["max_usd_per_run"], run_id, "maps")
    except Exception as e:
        C.log("maps", "Google Maps (Apify)", query=f"{industry} @ {area}", errors=str(e), run_id=run_id)
        return {}
    rows = C.load()
    stats = {"raw": len(items), "new": 0, "merged": 0, "off_industry": 0, "closed": 0, "over_limit": 0}
    for it in items:
        if it.get("permanentlyClosed"):
            stats["closed"] += 1
            continue
        ind = _industry_from_item(it)
        if ind != industry and not _matches_industry(it, c["maps"]["category_words"][industry]):
            stats["off_industry"] += 1
            continue
        if stats["new"] >= want:
            stats["over_limit"] += 1
            continue
        lead = {"brand_name": it.get("title", ""), "industry": industry, "city": it.get("city", ""),
                "county": it.get("state", ""), "address": it.get("address", ""), "origin": "maps",
                "caen_note": f"industry from Google category '{it.get('categoryName', '')}'"}
        lead.update(_gbp_fields(it))
        existing = C.find(rows, lead)
        if existing is not None and existing.get("website_url") and not lead.get("website_url"):
            lead.pop("website_status", None)
            lead.pop("website_note", None)
        _, new = C.upsert(rows, lead)
        stats["new" if new else "merged"] += 1
    C.save(rows)
    C.log("maps", "Google Maps (Apify)", query=f"{', '.join(terms)} @ {area}", results=len(items), run_id=run_id,
          errors="" if status == "SUCCEEDED" else f"actor status {status}",
          note=f"cost {usd_spent:.3f} USD; " + ", ".join(f"{k} {v}" for k, v in stats.items()))
    return stats


def lookup(lead_ids=None, usd=None, limit=40, run_id=None):
    """GBP lookup for register leads without one (one Maps search per lead, top result only)."""
    c = C.cfg()
    run_id = run_id or C.new_run_id("gbp")
    rows = C.load()
    todo = [r for r in rows if (not lead_ids or r["lead_id"] in lead_ids) and r["verification_status"] != "DISCARDED"
            and (lead_ids or (not r["gbp_status"] and C.is_stale(r["gbp_checked_at"])))][:limit]
    if not todo:
        print("No leads need a GBP lookup.")
        return {}
    queries = {}
    for r in todo:
        nm = r["brand_name"] or C.norm_name(r["legal_name"]).upper()
        queries.setdefault(f"{nm} {r['city']}".strip(), []).append(r)
    actor_input = {"searchStringsArray": list(queries), "maxCrawledPlacesPerSearch": 1, "language": "ro",
                   "countryCode": "ro", "scrapeContacts": True, "maxReviews": 0, "maxImages": 0}
    try:
        items, spent, status = C.apify_run(c["apify"]["maps_actor"], actor_input,
                                           usd or min(1.0, c["limits"]["max_usd_per_run"]), run_id, "gbp")
    except Exception as e:
        C.log("gbp", "Google Maps (Apify)", query=f"{len(queries)} name+city searches", errors=str(e), run_id=run_id)
        return {}
    by_query = {}
    for it in items:
        q = it.get("searchString") or ""
        by_query.setdefault(q, it)
    stats = {"FOUND": 0, "AMBIGUOUS": 0, "NOT_FOUND": 0}
    for q, rs in queries.items():
        it = by_query.get(q)
        for r in rs:
            if not it:
                C.merge(r, {"gbp_status": "NOT_FOUND", "gbp_checked_at": C.now_iso(),
                            "notes": f"GBP: no Google Maps result for '{q}' on {C.today()}"})
                stats["NOT_FOUND"] += 1
                continue
            same_name = any(C.names_match(it.get("title", ""), x) for x in (r["brand_name"], r["legal_name"]) if x)
            same_phone = C.norm_phone(r["phone"]) and C.norm_phone(r["phone"]) == C.norm_phone(
                it.get("phoneUnformatted") or it.get("phone"))
            key = C.city_key(r["city"])
            same_city = bool(key) and key in C.fold((it.get("city") or "") + " " + (it.get("address") or ""))
            if same_phone or (same_name and same_city):
                upd = _gbp_fields(it)
                upd["identity_evidence"] = " | ".join(x for x in (
                    upd.get("identity_evidence"), "GBP phone = registry phone" if same_phone else "GBP name+city match") if x)
                if not r["brand_name"] and it.get("title"):
                    upd["brand_name"] = it["title"]
                C.merge(r, upd)
                stats["FOUND"] += 1
            else:
                C.merge(r, {"gbp_status": "AMBIGUOUS", "gbp_checked_at": C.now_iso(),
                            "notes": f"GBP: top result '{it.get('title')}' ({it.get('url')}) does not clearly match"})
                stats["AMBIGUOUS"] += 1
    C.save(rows)
    C.log("gbp", "Google Maps (Apify)", query=f"{len(queries)} name+city searches", results=len(items), run_id=run_id,
          errors="" if status == "SUCCEEDED" else f"actor status {status}",
          note=f"cost {spent:.3f} USD; " + ", ".join(f"{k} {v}" for k, v in stats.items()))
    return stats

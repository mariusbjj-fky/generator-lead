"""Lead categories (1, 2A, 2B, 3) and the 0–100 priority score. Pure computation, no network.

Cat 1  verified new business, registered within `new_business_days`.
Cat 2  most recent post across ALL found platforms older than `inactive_days`:
       2A no active Meta ads, 2B active Meta ads. Active profiles (2C) are not leads.
       Any found platform without a readable date → UNKNOWN (not classified).
Cat 3  no website (verified NONE), broken/parked site, social-only, or almost no digital presence.
"""
from . import core as C

NO_SITE = {"NONE", "BROKEN", "PARKED", "SOCIAL_ONLY"}


def activity(r):
    found, dates, unknown = [], [], []
    for net, col in C.SOCIAL:
        if not r[col]:
            continue
        found.append(net)
        st, last = r[f"{net}_status"], r[f"{net}_last_post"]
        if last == "NO POSTS":
            dates.append((net, None))
        elif C.parse_date(last):
            dates.append((net, C.parse_date(last)))
        else:
            unknown.append(f"{net}:{st or 'not checked'}")
    if not found:
        return {"activity_status": "NO_PROFILES", "last_activity_date": "", "days_since_activity": ""}
    real = [d for _, d in dates if d]
    newest = max(real) if real else None
    lim = C.cfg()["windows"]["inactive_days"]
    days = (C.today() - newest).days if newest else ""
    if newest and days < lim:
        st = "ACTIVE"
    elif unknown:
        st = "UNKNOWN"
    else:
        st = "INACTIVE"
    return {"activity_status": st, "last_activity_date": newest.isoformat() if newest else
            ("NO POSTS" if dates and not unknown else "UNKNOWN"), "days_since_activity": days,
            "_unknown": unknown}


def weak_presence(r):
    """Almost no digital presence: no working site, no social profiles, and ≤3 Google reviews or no GBP."""
    reviews = int(r["gbp_reviews"]) if str(r["gbp_reviews"]).isdigit() else None
    no_gbp = r["gbp_status"] in ("NOT_FOUND",)
    return (r["website_status"] in NO_SITE and not any(r[c] for _, c in C.SOCIAL)
            and (no_gbp or (reviews is not None and reviews <= 3)))


def classify(r):
    c = C.cfg()
    s = c["scoring"]
    out = activity(r)
    unknown = out.pop("_unknown", [])
    cats, pts, notes = [], [], []
    reg = C.parse_date(r["registration_date"])
    if (reg and (C.today() - reg).days <= c["windows"]["new_business_days"] and r["verification_status"] == "VERIFIED"
            and "cat1" in r["origin"]):
        cats.append("1")
        pts.append(("new business ≤%dd" % c["windows"]["new_business_days"], s["new_business"]))
    if out["activity_status"] == "INACTIVE":
        ads = r["meta_ads_status"] or "NOT CHECKED"
        if ads == "ACTIVE":
            cats.append("2B")
            pts.append(("inactive but running Meta ads", s["inactive_with_ads"]))
        elif ads == "NOT ACTIVE":
            cats.append("2A")
            pts.append(("inactive, no Meta ads", s["inactive_no_ads"]))
        else:
            notes.append("inactive 60d+ but Meta ads NOT CHECKED → not classified 2A/2B")
    elif out["activity_status"] == "UNKNOWN":
        notes.append("activity UNKNOWN (" + ", ".join(unknown) + ")")
    if r["website_status"] in NO_SITE or weak_presence(r):
        cats.append("3")
        why = "weak presence" if r["website_status"] not in NO_SITE else f"website {r['website_status']}"
        pts.append((f"no working website ({why})", s["no_website"]))
    elif r["website_status"] == "NOT_FOUND_IN_SOURCES":
        notes.append("no website in sources yet; web search pending before Cat 3")
    ind = c["industries"].get(r["industry"])
    if ind:
        pts.append((f"industry {r['industry']}", ind["score"]))
    if r["email"] or r["phone"]:
        pts.append(("public business contact", s["public_contact"]))
    if C.is_large_city(r["city"], r["county"]):
        pts.append(("large city", s["large_city"]))
    score = min(100, sum(p for _, p in pts)) if cats else 0
    pr = "HIGH" if score >= s["high_threshold"] else "MEDIUM" if score >= s["medium_threshold"] else "LOW"
    out.update({
        "categories": ", ".join(cats), "score": score if cats else "", "priority": pr if cats else "",
        "score_breakdown": (" + ".join(f"{n} {p:+d}" for n, p in pts) + f" = {score}") if cats else "; ".join(notes),
        "checked_at": C.now_iso(),
    })
    if cats and notes:
        out["score_breakdown"] += " | " + "; ".join(notes)
    return out


def run():
    rows = C.load()
    counts = {}
    for r in rows:
        if r["verification_status"] == "DISCARDED" or ("cat1" in r["origin"] and r["verification_status"] != "VERIFIED"):
            # Registry-only companies not confirmed by a second source are not leads.
            C.merge(r, {"categories": "", "score": "", "priority": "", "checked_at": C.now_iso(),
                        "score_breakdown": f"not scored: verification {r['verification_status'] or 'pending'}"})
            continue
        upd = classify(r)
        C.merge(r, upd)
        for cat in filter(None, (x.strip() for x in upd["categories"].split(","))):
            counts[cat] = counts.get(cat, 0) + 1
    C.save(rows)
    print("Leads per category:", counts or "none")
    return counts

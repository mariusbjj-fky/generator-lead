"""JSON feed for the static viewer in web/ (python run.py web → web/data/leads.json)."""
import json
import os

from . import core as C
from . import export as X

FIELDS = [
    "lead_id", "priority", "score", "categories", "legal_name", "brand_name", "industry", "caen_code", "caen_version",
    "city", "county", "registration_date", "cui", "verification_status", "website_url", "website_status",
    "website_note", "gbp_url", "gbp_status", "gbp_reviews", "gbp_rating", "facebook_url", "facebook_last_post",
    "instagram_url", "instagram_last_post", "tiktok_url", "tiktok_last_post", "last_activity_date",
    "days_since_activity", "activity_status", "meta_ads_status", "meta_ads_count", "ad_library_url", "email",
    "email_source", "phone", "phone_source", "score_breakdown", "notes", "conflicts", "identity_evidence",
    "sources", "checked_at",
]


def build(path=None):
    path = path or os.path.join(C.ROOT, "web", "data", "leads.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rows = C.load()
    review = {r["lead_id"] for r in X.needs_review(rows)}
    leads = []
    for r in rows:
        if r["verification_status"] == "DISCARDED":
            continue
        if not r["categories"] and r["lead_id"] not in review:
            continue  # active / out-of-scope businesses are not shown
        d = {k: r.get(k, "") for k in FIELDS}
        d["score"] = int(r["score"]) if str(r["score"]).isdigit() else None
        d["industry_label"] = C.cfg()["industries"].get(r["industry"], {}).get("label", r["industry"])
        d["needs_review"] = r["lead_id"] in review
        leads.append(d)
    leads.sort(key=lambda d: d["score"] if d["score"] is not None else -1, reverse=True)
    data = {
        "generated_at": C.now_iso(),
        "summary": X.summary_lines(rows, C.load_log(C.session_id())),
        "leads": leads,
        "run_log": C.load_log(C.session_id()),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"Viewer data: {path} ({len(leads)} leads)")
    return path

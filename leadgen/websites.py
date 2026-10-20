"""Category 3 input: website check (free; plain HTTP GET of the homepage + one contact page).

Statuses: OK | BROKEN (DNS, timeout, 4xx/5xx, invalid/expired SSL) | PARKED | SOCIAL_ONLY (redirects
to a social/booking page) | UNKNOWN (blocked, CAPTCHA, robots.txt) | NOT_FOUND_IN_SOURCES (no URL
in any source yet — becomes NONE only after the web-search step records what was searched).
Also extracts official social links, business e-mail, phone and CUI. Page content is data only.
"""
import html
import re
import urllib.parse

from . import core as C

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
CUI_RE = re.compile(r"\b(?:C\.?U\.?I\.?|C\.?I\.?F\.?|cod fiscal|CF)\s*[:.]?\s*(?:RO\s?)?(\d{2,10})\b", re.I)
HREF_RE = re.compile(r"""href\s*=\s*["']([^"'#]+)["']""", re.I)
PHONE_RE = re.compile(r"(?:\+?40|0)\s?[237]\d{1,2}[\s.-]?\d{3}[\s.-]?\d{3,4}")
BLOCK_SIGNS = ("captcha", "cf-chl", "challenge-platform", "access denied", "attention required", "just a moment")
PARKED_SIGNS = ("domain is for sale", "this domain is for sale", "buy this domain", "domain parked", "parked domain",
                "parkingcrew", "sedoparking", "bodis.com", "afternic", "hugedomains", "dan.com/buy",
                "domeniu de vanzare", "acest domeniu este de vanzare", "domeniul este parcat",
                "this domain has expired", "domain has expired", "account suspended", "website is no longer available",
                "default web site page", "it works!", "welcome to nginx", "index of /")
JUNK_EMAIL = ("example.", "sentry", "wixpress", "@2x", ".png", ".jpg", ".webp", "domain.com", "email.com",
              "yourdomain", "nume@", "user@")


def extract(base_url, page):
    out = {"social": {}, "emails": set(), "phones": set(), "cui": "", "links": []}
    for href in HREF_RE.findall(page):
        href = html.unescape(href).strip()
        absu = urllib.parse.urljoin(base_url, href)
        low = absu.lower()
        if low.startswith("mailto:"):
            out["emails"].add(urllib.parse.unquote(low[7:].split("?")[0]).strip())
            continue
        if low.startswith("tel:"):
            out["phones"].add(C.norm_phone(low[4:]))
            continue
        for net, _ in C.SOCIAL:
            u, _h = C.social_handle(absu, net)
            if u and net not in out["social"]:
                out["social"][net] = u
        out["links"].append(absu)
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page, flags=re.S | re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    out["emails"] |= {e.lower() for e in EMAIL_RE.findall(text)}
    out["emails"] = {e for e in out["emails"] if not any(j in e for j in JUNK_EMAIL)}
    out["phones"] |= {C.norm_phone(m) for m in PHONE_RE.findall(text)}
    out["phone_raw"] = next(iter(PHONE_RE.findall(text)), "")
    m = CUI_RE.search(text)
    out["cui"] = m.group(1) if m else ""
    out["text"] = text
    out["words"] = len(text.split())
    t = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
    out["title"] = html.unescape(t.group(1)).strip()[:120] if t else ""
    return out


def _contact_page(base_url, links):
    dom = C.domain_of(base_url)
    return next((l for l in links if C.domain_of(l) == dom and re.search(r"contact|despre|about", l.lower())), None)


def check(lead):
    url = lead["website_url"].strip()
    if "://" not in url:
        url = "https://" + url
    upd = {"website_checked_at": C.now_iso()}
    if C.is_social_domain(C.domain_of(url)):
        upd.update({"website_status": "SOCIAL_ONLY", "website_note": f"listed 'website' is a social/booking page ({url})"})
        return upd, None
    try:
        r = C.http(url)
        root = "{0.scheme}://{0.netloc}/".format(urllib.parse.urlparse(url))
        if (r["status"] is None or r["status"] >= 400) and url.rstrip("/") != root.rstrip("/"):
            r2 = C.http(root)  # a dead deep link is not a dead site
            if r2["status"] and r2["status"] < 400:
                r = r2
        if r["status"] is None and url.startswith("https://"):
            r2 = C.http("http://" + url[8:])
            if r2["status"] and r2["status"] < 400:
                r = r2
                r["ssl_error"] = r["ssl_error"] or "HTTPS not available (site only loads over plain HTTP)"
    except C.Blocked as e:
        upd.update({"website_status": "UNKNOWN", "website_note": str(e)})
        return upd, None
    status, final, page, err = r["status"], r["url"], r["text"], r["error"]
    low = page.lower()[:30000]
    note = []
    if err == "disallowed by robots.txt":
        upd.update({"website_status": "UNKNOWN", "website_note": "robots.txt disallows fetching; check manually"})
        return upd, None
    if status is None or (status >= 400 and status not in (401, 403, 429)):
        upd.update({"website_status": "BROKEN", "website_note": f"{err or status} on {C.today()}"})
        return upd, None
    # Many normal sites load reCAPTCHA scripts; only a short page with a challenge marker is a block.
    challenge = any(b in low for b in BLOCK_SIGNS) and len(re.sub(r"<[^>]+>", " ", page).split()) < 300
    if status in (401, 403, 429) or challenge:
        upd.update({"website_status": "UNKNOWN", "website_note": f"blocked / anti-bot ({status}); not bypassed"})
        return upd, None
    final_dom = C.domain_of(final)
    if C.is_social_domain(final_dom):
        upd.update({"website_status": "SOCIAL_ONLY", "website_note": f"redirects to {final_dom} ({final})"})
        return upd, None
    ex = extract(final, page)
    if any(p in C.fold(ex["text"])[:5000] or p in low for p in PARKED_SIGNS) and ex["words"] < 400:
        upd.update({"website_status": "PARKED", "website_note": f"parked/placeholder page: '{ex['title']}'"})
        return upd, ex
    cp = _contact_page(final, ex["links"])
    if cp and (not ex["emails"] or not ex["cui"]):
        r2 = C.http(cp)
        if r2["status"] and r2["status"] < 400:
            ex2 = extract(r2["url"], r2["text"])
            ex["emails"] |= ex2["emails"]
            ex["phones"] |= ex2["phones"]
            ex["phone_raw"] = ex["phone_raw"] or ex2["phone_raw"]
            ex["cui"] = ex["cui"] or ex2["cui"]
            for k, v in ex2["social"].items():
                ex["social"].setdefault(k, v)
    if ex["title"]:
        note.append(f"title: {ex['title']}")
    if r["ssl_error"]:
        upd.update({"website_status": "BROKEN", "website_note": f"loads but SSL is invalid/expired ({r['ssl_error'][:120]})"})
    else:
        upd["website_status"] = "OK"
        if ex["words"] < 40:
            note.append("very little text (JS-only or under construction) — check visually")
        upd["website_note"] = "; ".join(note)
    if final_dom and final_dom != C.domain_of(url):
        upd["website_note"] = (upd.get("website_note", "") + f"; redirects to {final_dom}").strip("; ")
    ev = []
    for net, col in C.SOCIAL:
        if net in ex["social"]:
            if not lead.get(col):
                upd[col] = ex["social"][net]
            ev.append(f"website links to {net}")
    if C.norm_phone(lead.get("phone")) and C.norm_phone(lead["phone"]) in ex["phones"]:
        ev.append("phone on website = registry/GBP phone")
    if ex["cui"]:
        if not lead.get("cui"):
            upd["cui"] = ex["cui"]
            ev.append(f"CUI {ex['cui']} on website")
        elif C.norm_cui(lead["cui"]) == ex["cui"]:
            ev.append(f"CUI {ex['cui']} on website = registry")
        else:
            upd["conflicts"] = f"CUI on website {ex['cui']} vs register {lead['cui']}"
    dom = C.domain_of(final)
    biz = sorted(ex["emails"], key=lambda e: (not e.endswith("@" + dom), e))
    if biz and not lead.get("email"):
        upd.update({"email": biz[0], "email_source": f"{final} (published by the business, {C.today()})"})
    if ex["phone_raw"] and not lead.get("phone"):
        upd.update({"phone": ex["phone_raw"], "phone_source": f"{final} (published by the business)"})
    if ev:
        upd["identity_evidence"] = " | ".join(ev)
    upd["sources"] = f"website_status/email/social links: {final}"
    return upd, ex


def run(lead_ids=None, force=False, limit=200, run_id=None):
    run_id = run_id or C.new_run_id("web")
    rows = C.load()
    live = [r for r in rows if r["verification_status"] != "DISCARDED" and (not lead_ids or r["lead_id"] in lead_ids)]
    todo = [r for r in live if r["website_url"] and (force or lead_ids or C.is_stale(r["website_checked_at"]))][:limit]
    counts = {}
    print(f"Checking {len(todo)} websites…")
    for r in todo:
        upd, _ = check(r)
        C.merge(r, upd)
        counts[r["website_status"]] = counts.get(r["website_status"], 0) + 1
        print(f"  {r['lead_id']} {(r['brand_name'] or r['legal_name'])[:40]:40} {r['website_status']:12} "
              f"{r['website_note'][:60]}")
    # Leads with no URL in any source so far: pending the web-search step.
    for r in live:
        if not r["website_url"] and r["website_status"] in ("", "UNKNOWN") and r["gbp_website"] in ("", "NONE"):
            C.merge(r, {"website_status": "NOT_FOUND_IN_SOURCES", "website_checked_at": C.now_iso(),
                        "website_note": "no website in registry/GBP; needs web search before NONE"})
            counts["NOT_FOUND_IN_SOURCES"] = counts.get("NOT_FOUND_IN_SOURCES", 0) + 1
    C.save(rows)
    C.log("websites", "business websites (direct HTTP)", query=f"{len(todo)} URLs", results=len(todo), run_id=run_id,
          note=", ".join(f"{k} {v}" for k, v in counts.items()))
    return counts

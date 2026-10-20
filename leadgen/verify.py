"""Cross-verification of companies by CUI.

Primary second source: ANAF public web service (PlatitorTvaRest v9; official, free, ≤100 CUIs per call,
1 call/s). listafirme.ro is tried once per run; it sits behind a Cloudflare challenge, which is never
bypassed — when blocked, a manual-check link is recorded instead.
A Category 1 lead that no second source confirms is marked DISCARDED (kept in the register, not exported).
"""
import json

from . import core as C


def _anaf(cuis, run_id):
    url = C.cfg()["sources"]["anaf_api"]
    found, missing = {}, set()
    for i in range(0, len(cuis), 100):
        chunk = cuis[i:i + 100]
        body = [{"cui": int(c), "data": C.today().isoformat()} for c in chunk]
        r = C.http(url, method="POST", body=body, api=True, timeout=40)
        if r["status"] != 200:
            C.log("verify", "ANAF API", query=f"{len(chunk)} CUIs", errors=r["error"] or str(r["status"]),
                  blocked="yes" if r["status"] in (403, 429) else "", run_id=run_id)
            continue
        d = json.loads(r["text"])
        for it in d.get("found", []):
            found[str(it["date_generale"]["cui"])] = it
        missing |= {str(x) for x in d.get("notFound", [])}
        C.log("verify", "ANAF API", query=f"{len(chunk)} CUIs", results=len(d.get("found", [])), run_id=run_id,
              note=f"not found: {len(d.get('notFound', []))}")
    return found, missing


def _listafirme_probe(cui, run_id):
    url = C.cfg()["sources"]["listafirme_search"].format(q=cui)
    try:
        r = C.http(url, timeout=20)
    except C.Blocked as e:
        r = {"status": 403, "text": "", "error": str(e)}
    body = (r.get("text") or "")[:3000].lower()
    blocked = r["status"] in (403, 429, 503) or "just a moment" in body or "challenge" in body
    C.log("verify", "listafirme.ro", query=f"search {cui}", results=0 if blocked else 1, run_id=run_id,
          blocked="Cloudflare challenge" if blocked else "", errors=r.get("error", ""),
          note="not bypassed; manual-check link stored per lead" if blocked else "page reachable")
    return not blocked


def verify(lead_ids=None, run_id=None, force=False):
    run_id = run_id or C.new_run_id("verify")
    rows = C.load()
    todo = [r for r in rows if C.norm_cui(r["cui"]) and (not lead_ids or r["lead_id"] in lead_ids)
            and (force or lead_ids or r["verification_status"] in ("", "UNVERIFIED"))]
    if not todo:
        print("Nothing to verify.")
        return {}
    print(f"Verifying {len(todo)} companies with ANAF…")
    found, missing = _anaf(sorted({C.norm_cui(r["cui"]) for r in todo}), run_id)
    _listafirme_probe(C.norm_cui(todo[0]["cui"]), run_id)
    stats = {"VERIFIED": 0, "CONFLICT": 0, "DISCARDED": 0, "UNVERIFIED": 0}
    for r in todo:
        cui = C.norm_cui(r["cui"])
        lf = C.cfg()["sources"]["listafirme_search"].format(q=cui)
        upd = {"checked_at": C.now_iso(), "sources": f"manual check (Cloudflare-protected): {lf}"}
        it = found.get(cui)
        if it is None:
            if cui in missing:
                upd.update({"verification_status": "DISCARDED",
                            "notes": f"ANAF: CUI {cui} not found on {C.today()}"})
            else:
                upd.update({"verification_status": "UNVERIFIED", "notes": "ANAF: no answer for this CUI (retry)"})
            C.merge(r, upd)
            stats[upd["verification_status"]] += 1
            continue
        g = it["date_generale"]
        inactive = it.get("stare_inactiv") or {}
        anaf_src = f"ANAF API {C.cfg()['sources']['anaf_api']} (CUI {cui}, {C.today()})"
        problems = []
        if r["legal_name"] and not C.names_match(r["legal_name"], g.get("denumire", "")):
            problems.append(f"name: ONRC '{r['legal_name']}' vs ANAF '{g.get('denumire')}'")
        anaf_reg = C.parse_date(g.get("data_inregistrare"))
        reg = C.parse_date(r["registration_date"])
        if anaf_reg and reg and abs((anaf_reg - reg).days) > 5:
            problems.append(f"registration_date: {reg} vs ANAF {anaf_reg}")
        stare = g.get("stare_inregistrare", "")
        dead = (not stare.upper().startswith("INREGISTRAT") or inactive.get("statusInactivi")
                or inactive.get("dataRadiere"))
        principal = (g.get("cod_CAEN") or "").strip()
        codes = [x.strip() for x in r["caen_code"].split(",") if x.strip()]
        ind, why = C.industry_of(r["legal_name"] or g.get("denumire", ""), codes, principal or None)
        upd.update({
            "legal_name": r["legal_name"] or g.get("denumire", ""),
            "company_status": f"ANAF: {stare}" + (" | INACTIV fiscal" if inactive.get("statusInactivi") else ""),
            "verified_by": f"ANAF {C.today()}",
            "sources": f"cui/name/status/caen: {anaf_src}",
        })
        if not r["registration_date"] and anaf_reg:
            upd["registration_date"] = anaf_reg.isoformat()
        if principal:
            if principal not in codes:
                r["caen_code"] = ", ".join([principal] + codes)  # principal first; not a conflict
            upd["caen_note"] = f"principal CAEN {principal} (ANAF); {why}"
        if g.get("telefon") and not r["phone"]:
            upd.update({"phone": g["telefon"], "phone_source": anaf_src})
        if not r["address"] and g.get("adresa"):
            upd["address"] = g["adresa"]
        cat1 = "cat1" in r["origin"]
        new_ind = ((ind or r["industry"]) if cat1 else (r["industry"] or ind)) or ""
        if dead:
            upd.update({"verification_status": "DISCARDED", "notes": f"ANAF status: {stare or 'radiat/inactiv'}"})
        elif not ind and cat1:
            upd.update({"verification_status": "DISCARDED",
                        "notes": f"out of scope after ANAF check: principal CAEN {principal or '-'}; {why}"})
        elif problems:
            upd.update({"verification_status": "CONFLICT", "conflicts": " | ".join(problems), "industry": new_ind})
        else:
            upd.update({"verification_status": "VERIFIED", "industry": new_ind})
        C.merge(r, upd)
        stats[upd["verification_status"]] += 1
    C.save(rows)
    C.log("verify", "verification summary", results=sum(stats.values()), run_id=run_id,
          note=", ".join(f"{k} {v}" for k, v in stats.items()))
    return stats

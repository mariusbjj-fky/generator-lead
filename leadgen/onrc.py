"""Category 1 discovery: newly registered businesses from ONRC open data (data.gov.ro).

ONRC's own portal (myportal.onrc.ro) needs an account, so it is not used; the limitation is logged.
The open-data snapshot ("Firme înregistrate la Registrul Comerțului până la data de …") is streamed:
OD_FIRME (company + registration date) → OD_CAEN_AUTORIZAT (authorised activities) → OD_STARE_FIRMA.
Only companies registered within `onrc_cache_days` are kept in a local cache per snapshot, so a
second run on the same snapshot costs nothing.
"""
import io
import json
import os
import re
import urllib.request
from datetime import date, timedelta

from . import core as C


def _target_codes():
    codes = set()
    for ind in C.cfg()["industries"].values():
        codes |= set(ind["caen_strict"]) | set(ind["caen_with_keyword"])
    return codes


def latest_snapshot():
    """Find the newest 'firme-DD-MM-YYYY' and 'nomenclatoare-…' datasets via the CKAN API."""
    url = C.cfg()["sources"]["ckan_search"] + "?q=onrc&rows=20&sort=metadata_modified%20desc"
    r = C.http(url, api=True, timeout=30)
    if r["status"] != 200:
        raise RuntimeError(f"data.gov.ro CKAN API: {r['error'] or r['status']}")
    res = json.loads(r["text"])["result"]["results"]
    firme = next(p for p in res if re.match(r"firme-\d{2}-\d{2}-\d{4}$", p["name"]))
    nomen = next((p for p in res if p["name"] == firme["name"].replace("firme", "nomenclatoare")), None)
    d, m, y = firme["name"].split("-")[1:]
    files = {x["name"].upper(): x["url"] for x in firme["resources"]}
    if nomen:
        files.update({x["name"].upper(): x["url"] for x in nomen["resources"]})
    return {"name": firme["name"], "date": date(int(y), int(m), int(d)), "files": files,
            "page": f"https://data.gov.ro/dataset/{firme['name']}"}


def _stream(url):
    req = urllib.request.Request(url, headers={"User-Agent": C.cfg()["user_agent"]})
    resp = urllib.request.urlopen(req, timeout=120)
    return io.TextIOWrapper(resp, encoding="utf-8-sig", errors="replace", newline="")


def _rows(url, label):
    """Yield dicts from a '^'-separated ONRC file (fields are not quoted)."""
    f = _stream(url)
    head = f.readline().rstrip("\r\n").split("^")
    n = 0
    for line in f:
        parts = line.rstrip("\r\n").split("^")
        n += 1
        if n % 1_000_000 == 0:
            print(f"  {label}: {n:,} rows…", flush=True)
        if len(parts) < len(head):
            continue
        yield dict(zip(head, parts))
    print(f"  {label}: {n:,} rows read", flush=True)


def build_cache(snap):
    path = os.path.join(C.data_dir("cache"), f"onrc_{snap['name']}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f), True
    since = snap["date"] - timedelta(days=C.cfg()["windows"]["onrc_cache_days"])
    firms = {}
    print(f"Streaming OD_FIRME from {snap['name']} (≈700 MB, a few minutes)…", flush=True)
    for r in _rows(snap["files"]["OD_FIRME.CSV"], "OD_FIRME"):
        d = C.parse_date(r.get("DATA_INMATRICULARE"))
        if not d or d < since:
            continue
        firms[r["COD_INMATRICULARE"]] = {
            "legal_name": r["DENUMIRE"].strip(), "cui": C.norm_cui(r["CUI"]), "reg_number": r["COD_INMATRICULARE"],
            "registration_date": d.isoformat(), "legal_form": r.get("FORMA_JURIDICA", ""),
            "county": r.get("ADR_JUDET", "").strip(), "city": r.get("ADR_LOCALITATE", "").strip(),
            "address": ", ".join(x.strip() for x in (r.get("ADR_DEN_STRADA"), r.get("ADR_NR_STRADA"))
                                 if x and x.strip()),
            "web": r.get("WEB", "").strip(), "caen": [], "caen_all": 0, "status": "",
        }
    print(f"  {len(firms):,} companies registered since {since}", flush=True)
    targets = _target_codes()
    print("Streaming OD_CAEN_AUTORIZAT (≈440 MB)…", flush=True)
    for r in _rows(snap["files"]["OD_CAEN_AUTORIZAT.CSV"], "OD_CAEN_AUTORIZAT"):
        f = firms.get(r["COD_INMATRICULARE"])
        if f is not None:
            f["caen_all"] += 1
            code = r["COD_CAEN_AUTORIZAT"].strip()
            if code in targets:
                f["caen"].append(f"{code}/v{r.get('VER_CAEN_AUTORIZAT', '').strip()}")
    status_names = {}
    if "N_STARE_FIRMA.CSV" in snap["files"]:
        status_names = {r["COD"]: r["DENUMIRE"] for r in _rows(snap["files"]["N_STARE_FIRMA.CSV"], "N_STARE_FIRMA")}
    print("Streaming OD_STARE_FIRMA (≈90 MB)…", flush=True)
    for r in _rows(snap["files"]["OD_STARE_FIRMA.CSV"], "OD_STARE_FIRMA"):
        f = firms.get(r["COD_INMATRICULARE"])
        if f is not None:
            name = status_names.get(r["COD"], r["COD"])
            f["status"] = ", ".join(x for x in (f["status"], name) if x)
    keep = {k: v for k, v in firms.items() if v["caen"]}  # only firms with a target activity are cached
    data = {"snapshot": snap["name"], "snapshot_date": snap["date"].isoformat(), "page": snap["page"],
            "firms_in_window": len(firms), "firms": keep}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    return data, False


def discover(days=None, run_id=None):
    c = C.cfg()
    days = days or c["windows"]["new_business_days"]
    run_id = run_id or C.new_run_id("new")
    start = C.today() - timedelta(days=days)
    C.log("cat1", "ONRC portal (myportal.onrc.ro)", results=0, blocked="login required",
          note="not used: account login required; not bypassed", run_id=run_id)
    C.log("cat1", "Monitorul Oficial Partea a IV-a", results=0, blocked="subscription",
          note="not queried automatically: full text needs a paid subscription", run_id=run_id)
    try:
        snap = latest_snapshot()
    except Exception as e:
        C.log("cat1", "data.gov.ro CKAN API", errors=str(e), run_id=run_id)
        return []
    data, cached = build_cache(snap)
    sd = date.fromisoformat(data["snapshot_date"])
    C.log("cat1", f"data.gov.ro ONRC open data {data['snapshot']}", query=f"registered {start}..{C.today()}",
          results=len(data["firms"]), run_id=run_id,
          note=("cache reused; " if cached else "") +
               f"{data['firms_in_window']} firms in {c['windows']['onrc_cache_days']}-day cache window, "
               f"{len(data['firms'])} with a target CAEN")
    if sd < C.today() - timedelta(days=1):
        C.log("cat1", "data.gov.ro ONRC open data", results="",
              note=f"LIMITATION: snapshot is up to {sd}; registrations after that date are not covered yet",
              run_id=run_id)
    rows = C.load()
    out = {"window": 0, "industry": 0, "status": 0, "new": 0, "merged": 0, "no_cui": 0}
    for f in data["firms"].values():
        reg = date.fromisoformat(f["registration_date"])
        if reg < start:
            continue
        out["window"] += 1
        codes = [x.split("/")[0] for x in f["caen"]]
        ind, why = C.industry_of(f["legal_name"], codes)
        if not ind:
            continue
        out["industry"] += 1
        st = C.fold(f["status"])
        if st and "functiune" not in st and "inregistrare" not in st:
            out["status"] += 1
            continue
        versions = sorted({x.split("/v")[1] for x in f["caen"] if "/v" in x})
        lead = {
            "cui": f["cui"], "legal_name": f["legal_name"], "industry": ind, "reg_number": f["reg_number"],
            "caen_code": ", ".join(dict.fromkeys(codes)),
            "caen_version": "Rev " + "/".join({"2": "2", "3": "3"}.get(v, v) for v in versions),
            "caen_note": f"authorised activity in ONRC open data ({f['caen_all']} codes total); {why}",
            "registration_date": f["registration_date"], "company_status": f["status"] or "funcțiune",
            "city": f["city"], "county": f["county"], "address": f["address"],
            "verification_status": "UNVERIFIED", "origin": "cat1:onrc_open_data",
            "sources": f"registration_date/caen/status: {data['page']}",
        }
        if f["web"]:
            lead["website_url"] = f["web"] if "://" in f["web"] else "http://" + f["web"]
            lead["sources"] += f" | website_url: ONRC open data WEB field"
        if not f["cui"]:
            out["no_cui"] += 1
            lead["notes"] = "ONRC open data has no CUI yet; verify manually"
        _, new = C.upsert(rows, lead)
        out["new" if new else "merged"] += 1
    C.save(rows)
    C.log("cat1", "ONRC filter", query=f"last {days} days, target CAEN + keyword rules",
          results=out["new"] + out["merged"], run_id=run_id,
          note=f"in window {out['window']}, industry match {out['industry']}, not in operation {out['status']}, "
               f"new {out['new']}, already known {out['merged']}, missing CUI {out['no_cui']}")
    return out

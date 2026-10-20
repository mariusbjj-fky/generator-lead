"""Shared helpers: config, time, normalisation, lead register, run log, polite HTTP, Apify.

The register is one CSV (UTF-8 with BOM so Excel shows diacritics) at <data_dir>/leads.csv.
Every save keeps a timestamped backup. Stdlib only (openpyxl is used just for the Excel export).
Nothing in this package ever contacts a lead.
"""
import csv
import json
import os
import re
import shutil
import ssl
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

COLUMNS = [
    # identity
    "lead_id", "cui", "legal_name", "brand_name", "industry", "caen_code", "caen_version", "caen_note",
    "reg_number", "registration_date", "company_status", "city", "county", "address",
    "verification_status", "verified_by",
    # website / Google Business Profile
    "website_url", "website_status", "website_note", "website_checked_at", "website_search_note",
    "gbp_url", "gbp_status", "gbp_website", "gbp_reviews", "gbp_rating", "gbp_checked_at",
    # social
    "facebook_url", "facebook_status", "facebook_last_post", "facebook_checked_at",
    "instagram_url", "instagram_status", "instagram_last_post", "instagram_checked_at",
    "tiktok_url", "tiktok_status", "tiktok_last_post", "tiktok_checked_at",
    "social_search_note", "identity_evidence",
    "last_activity_date", "days_since_activity", "activity_status",
    # ads
    "meta_ads_status", "meta_ads_count", "ad_library_url", "ads_checked_at",
    # contact (business only)
    "email", "email_source", "phone", "phone_source",
    # result
    "categories", "score", "priority", "score_breakdown",
    "sources", "conflicts", "notes", "origin", "checked_at", "discovered_at", "updated_at",
]

# Allowed values; `run.py set` rejects anything else.
ALLOWED = {
    "verification_status": ["VERIFIED", "UNVERIFIED", "CONFLICT", "DISCARDED"],
    "website_status": ["OK", "NONE", "BROKEN", "PARKED", "SOCIAL_ONLY", "NOT_FOUND_IN_SOURCES", "UNKNOWN"],
    "gbp_status": ["FOUND", "NOT_FOUND", "AMBIGUOUS", "NOT_CHECKED"],
    "facebook_status": ["FOUND", "NOT_FOUND", "PRIVATE", "ERROR"],
    "instagram_status": ["FOUND", "NOT_FOUND", "PRIVATE", "ERROR"],
    "tiktok_status": ["FOUND", "NOT_FOUND", "PRIVATE", "ERROR"],
    "meta_ads_status": ["ACTIVE", "NOT ACTIVE", "NOT CHECKED"],
    "activity_status": ["ACTIVE", "INACTIVE", "UNKNOWN", "NO_PROFILES"],
}
# Fields that describe the current observation: a newer check overwrites them.
REFRESH = {
    "company_status", "verification_status", "industry", "caen_note", "website_status", "website_note", "website_checked_at",
    "gbp_status", "gbp_website", "gbp_reviews", "gbp_rating", "gbp_checked_at",
    "facebook_status", "facebook_last_post", "facebook_checked_at",
    "instagram_status", "instagram_last_post", "instagram_checked_at",
    "tiktok_status", "tiktok_last_post", "tiktok_checked_at",
    "last_activity_date", "days_since_activity", "activity_status",
    "meta_ads_status", "meta_ads_count", "ad_library_url", "ads_checked_at",
    "categories", "score", "priority", "score_breakdown", "checked_at",
}
# Fields that accumulate (separator " | ").
APPEND = {"sources", "conflicts", "notes", "identity_evidence", "verified_by", "origin"}
SOCIAL = (("facebook", "facebook_url"), ("instagram", "instagram_url"), ("tiktok", "tiktok_url"))


# ---------------------------------------------------------------- config / time
_CFG = None


def cfg():
    global _CFG
    if _CFG is None:
        with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
            _CFG = json.load(f)
    return _CFG


def tz():
    return ZoneInfo(cfg().get("timezone", "Europe/Bucharest"))


def now():
    return datetime.now(tz())


def now_iso():
    return now().isoformat(timespec="seconds")


def today():
    return now().date()


def parse_date(s):
    """Parse ISO / dd.mm.yyyy / dd/mm/yyyy (date or datetime). Returns date or None."""
    if not s:
        return None
    s = str(s).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        return (dt.astimezone(tz()) if dt.tzinfo else dt).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def days_since(s):
    d = parse_date(s)
    return None if d is None else (today() - d).days


def is_stale(checked_at, days=None):
    d = days_since(checked_at)
    return d is None or d >= (days if days is not None else cfg()["windows"]["recheck_days"])


def data_dir(*parts):
    d = cfg()["data_dir"]
    d = os.environ.get("LEADGEN_DATA_DIR") or (d if os.path.isabs(d) else os.path.join(ROOT, d))
    p = os.path.join(d, *parts)
    os.makedirs(p, exist_ok=True)
    return p


def new_run_id(kind):
    return f"{now().strftime('%Y%m%d-%H%M%S')}-{kind}"


# ---------------------------------------------------------------- normalisation
def strip_accents(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c))


def fold(s):
    """Lowercase, no diacritics (ş/ș/ţ/ț included), single spaces."""
    s = strip_accents(s).lower()
    return re.sub(r"\s+", " ", s).strip()


LEGAL_FORMS = r"\b(s\.?r\.?l\.?(-?d)?|s\.?a\.?|s\.?c\.?|s\.?n\.?c\.?|pfa|p\.?f\.?|i\.?i\.?|i\.?f\.?|cmi|srld)\b"


def norm_name(s):
    s = fold(s)
    s = re.sub(LEGAL_FORMS, " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def name_tokens(s):
    stop = {"clinica", "cabinet", "medical", "centrul", "centru", "de", "si", "the", "dr", "doctor"}
    return {t for t in norm_name(s).split() if len(t) > 2 and t not in stop}


def names_match(a, b):
    """Same business name after removing legal forms; or all distinctive tokens of the shorter shared."""
    na, nb = norm_name(a), norm_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return False
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if not small <= big:
        return False
    if len(small) >= 2:
        return True
    # A single shared word is only enough when it is distinctive and leads both names
    # ("Velisse" ~ "VELISSE BEAUTY MEDICAL CENTER SRL"), never for short generic words.
    word = next(iter(small))
    firsts = {norm_name(a).split()[0], norm_name(b).split()[0]}
    return len(word) >= 5 and firsts == {word}


def norm_phone(s):
    digits = re.sub(r"\D", "", s or "")
    return digits[-9:] if len(digits) >= 9 else ""


def norm_cui(s):
    m = re.search(r"(\d{2,10})", (str(s or "")).replace(" ", ""))
    return m.group(1) if m and m.group(1) != "0" else ""


def domain_of(url):
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    host = urllib.parse.urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


SOCIAL_HOSTS = ("instagram.com", "facebook.com", "fb.com", "fb.me", "tiktok.com", "linktr.ee", "linkedin.com",
                "youtube.com", "wa.me", "booksy.com", "fresha.com", "business.site", "g.page", "sites.google.com")


def is_social_domain(d):
    return any(d == h or d.endswith("." + h) for h in SOCIAL_HOSTS)


def social_handle(url, network):
    """Canonical profile URL + handle for instagram/facebook/tiktok links, or (None, None)."""
    if not url:
        return None, None
    u = url if "://" in url else "https://" + url
    p = urllib.parse.urlparse(u)
    host = re.sub(r"^(www\.|m\.|web\.|mobile\.|ro-ro\.)", "", p.netloc.lower())
    parts = [x for x in p.path.split("/") if x]
    bad = {"p", "reel", "reels", "explore", "stories", "share", "sharer", "sharer.php", "tr", "plugins",
           "dialog", "intent", "hashtag", "watch", "events", "groups", "login", "tv", "pages", "people",
           "privacy", "policies", "help", "video", "embed", "ads", "business", "legal", "about"}
    if network == "instagram" and host.endswith("instagram.com") and parts and parts[0].lower() not in bad:
        h = parts[0].lower()
        return f"https://www.instagram.com/{h}/", h
    if network == "facebook" and (host.endswith("facebook.com") or host.endswith("fb.com")):
        if parts and parts[0] == "profile.php":
            q = urllib.parse.parse_qs(p.query).get("id")
            return (f"https://www.facebook.com/profile.php?id={q[0]}", q[0]) if q else (None, None)
        if parts and parts[0] == "pages" and len(parts) >= 2:
            parts = parts[1:]
        if parts and parts[0].lower() not in bad:
            return f"https://www.facebook.com/{parts[0]}/", parts[0].lower()
    if network == "tiktok" and host.endswith("tiktok.com") and parts and parts[0].startswith("@"):
        h = parts[0][1:].lower()
        return f"https://www.tiktok.com/@{h}", h
    return None, None


def industry_of(text, caen_codes=(), principal=None):
    """Return (industry, reason) or (None, reason).

    Name/description keywords decide first (priority order). Without a keyword only the
    'caen_strict' codes count (e.g. 8623 is always dental); ambiguous codes such as 9602
    (also hair salons) or 4778 (any retail) need a keyword.
    """
    c = cfg()
    hay = " " + norm_name(text) + " "
    codes = [x for x in ([principal] if principal else []) + list(caen_codes) if x]
    for key in c["industry_priority"]:
        ind = c["industries"][key]
        kw = next((k for k in ind["keywords"] if fold(k) in hay), None)
        allowed = set(ind["caen_strict"]) | set(ind["caen_with_keyword"])
        if kw and (not codes or any(x in allowed for x in codes)):
            return key, f"keyword '{kw.strip()}'" + (f" + CAEN {next(x for x in codes if x in allowed)}" if codes else "")
    for key in c["industry_priority"]:
        ind = c["industries"][key]
        hit = next((x for x in codes if x in ind["caen_strict"]), None)
        if hit and (principal is None or principal == hit or principal in ind["caen_strict"]):
            return key, f"CAEN {hit}"
    return None, "no target CAEN/keyword"


def city_key(city):
    """'Bucureşti Sectorul 6' → 'bucuresti', 'Municipiul Cluj-Napoca' → 'cluj-napoca'."""
    s = re.sub(r"\b(sectorul|sector)\s*\d\b", " ", fold(city))
    s = re.sub(r"\b(municipiul|mun|orasul|oras|comuna|com|satul|sat|judetul|jud)\b\.?", " ", s)
    toks = [t for t in re.split(r"[\s,]+", s) if len(t) > 2]
    return toks[0] if toks else ""


def is_large_city(city, county=""):
    big = {city_key(c) for c in cfg()["large_cities"]}
    return city_key(city) in big or "bucuresti" in fold(county)


# ---------------------------------------------------------------- register
def register_path():
    return os.path.join(data_dir(), "leads.csv")


def load():
    path = register_path()
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for c in COLUMNS:
            r.setdefault(c, "")
    return rows


def save(rows):
    path = register_path()
    if os.path.exists(path):
        bdir = data_dir("backups")
        shutil.copy2(path, os.path.join(bdir, f"leads-{now().strftime('%Y%m%d-%H%M%S')}.csv"))
        for old in sorted(os.listdir(bdir))[:-30]:
            try:
                os.remove(os.path.join(bdir, old))
            except OSError:
                pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in COLUMNS})
    os.replace(tmp, path)


def next_id(rows):
    n = max([int(m.group(1)) for r in rows if (m := re.match(r"L-(\d+)", r.get("lead_id", "")))] or [0])
    return f"L-{n + 1:05d}"


def find(rows, new):
    """Dedupe: CUI first, then name + city when no CUI exists on either side."""
    cui = norm_cui(new.get("cui"))
    if cui:
        for r in rows:
            if norm_cui(r.get("cui")) == cui:
                return r
    nm = new.get("brand_name") or new.get("legal_name")
    city = fold(new.get("city"))
    if nm and city:
        for r in rows:
            if cui and norm_cui(r.get("cui")):
                continue  # both have CUIs and they differ → different companies
            if fold(r.get("city")) == city and any(
                    names_match(nm, x) for x in (r.get("brand_name"), r.get("legal_name")) if x):
                return r
    return None


def merge(row, new):
    for k, v in new.items():
        if k not in COLUMNS or v is None or (v == "" and k not in REFRESH):
            continue  # an empty value only clears a field that holds a current observation
        v = str(v)
        if k in APPEND:
            cur = [x for x in row.get(k, "").split(" | ") if x]
            for part in v.split(" | "):
                if part and part not in cur:
                    cur.append(part)
            row[k] = " | ".join(cur)
        elif k in REFRESH or not row.get(k):
            row[k] = v
        elif row.get(k) != v and k in ("cui", "phone", "email", "registration_date", "caen_code", "website_url"):
            # Keep the first value, record the conflict instead of overwriting.
            note = f"{k}: '{row[k]}' vs '{v}'"
            if note not in row.get("conflicts", ""):
                row["conflicts"] = " | ".join(x for x in (row.get("conflicts"), note) if x)
    row["updated_at"] = now_iso()


def upsert(rows, new):
    """Insert or merge one lead. Returns (row, created)."""
    match = find(rows, new)
    if match:
        merge(match, new)
        return match, False
    row = {c: "" for c in COLUMNS}
    row.update({"lead_id": next_id(rows), "discovered_at": now_iso(), "meta_ads_status": "NOT CHECKED"})
    merge(row, new)
    rows.append(row)
    return row, True


def add_source(row, field, url):
    merge(row, {"sources": f"{field}: {url}"})


# ---------------------------------------------------------------- run log
LOG_COLUMNS = ["timestamp", "session", "run_id", "step", "source", "query", "results", "errors", "blocked", "note"]


def session_id():
    """A session groups the commands of one research run (the Run Log tab shows the current one).
    Starts automatically; a new one begins after 12 h or with `run.py session`."""
    path = os.path.join(data_dir(), "session.json")
    try:
        with open(path, encoding="utf-8") as f:
            s = json.load(f)
        started = datetime.fromisoformat(s["started"])
        if now() - started < timedelta(hours=12):
            return s["id"]
    except Exception:
        pass
    return new_session()


def new_session():
    sid = now().strftime("S%Y%m%d-%H%M%S")
    with open(os.path.join(data_dir(), "session.json"), "w", encoding="utf-8") as f:
        json.dump({"id": sid, "started": now_iso()}, f)
    return sid


def log(step, source, query="", results="", errors="", blocked="", note="", run_id=""):
    path = os.path.join(data_dir(), "run_log.csv")
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(LOG_COLUMNS)
        w.writerow([now_iso(), session_id(), run_id, step, source, query, results, errors, blocked, note])
    flag = " BLOCKED" if blocked else ""
    print(f"[log] {step} | {source} | results={results}{flag} {('| ' + errors) if errors else ''}"
          f"{('| ' + note) if note else ''}", flush=True)


def load_log(session=None):
    path = os.path.join(data_dir(), "run_log.csv")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if session is None or r.get("session") == session]


# ---------------------------------------------------------------- polite HTTP
_last_hit = {}
_blocks = {}
_robots = {}


class Blocked(Exception):
    pass


def _robots_ok(url):
    if not cfg()["politeness"]["respect_robots_txt"]:
        return True
    p = urllib.parse.urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            req = urllib.request.Request(base + "/robots.txt", headers={"User-Agent": cfg()["user_agent"]})
            with urllib.request.urlopen(req, timeout=10, context=ssl._create_unverified_context()) as r:
                rp.parse(r.read(200_000).decode("utf-8", "replace").splitlines())
        except urllib.error.HTTPError as e:
            rp.parse([] if e.code in (404, 410) else ["User-agent: *", "Allow: /"])
        except Exception:
            rp.parse([])  # robots.txt unreachable → treat as allowed; the page fetch reports the real error
        _robots[base] = rp
    return _robots[base].can_fetch(cfg()["user_agent"], url)


def http(url, method="GET", body=None, headers=None, timeout=20, api=False, max_bytes=2_000_000,
         verify_ssl=True):
    """GET/POST with per-domain delay, robots.txt check (pages only), and back-off on 403/429.

    Returns dict(status, url, text, error, ssl_error). Raises Blocked when the domain keeps blocking
    (stop_source_after_blocks) so the caller can stop using that source.
    """
    pol = cfg()["politeness"]
    dom = domain_of(url)
    if _blocks.get(dom, 0) >= pol["stop_source_after_blocks"]:
        raise Blocked(f"{dom}: stopped after {_blocks[dom]} blocked responses")
    if not api and not _robots_ok(url):
        return {"status": None, "url": url, "text": "", "error": "disallowed by robots.txt", "ssl_error": ""}
    h = {"User-Agent": cfg()["user_agent"], "Accept-Language": "ro,en;q=0.8"}
    h.update(headers or {})
    data = json.dumps(body).encode() if body is not None and not isinstance(body, bytes) else body
    if data is not None:
        h.setdefault("Content-Type", "application/json")
    attempt = 0
    ssl_error = ""
    while True:
        wait = pol["min_delay_same_domain_s"] - (time.time() - _last_hit.get(dom, 0))
        if wait > 0:
            time.sleep(wait)
        _last_hit[dom] = time.time()
        ctx = ssl.create_default_context() if verify_ssl and not ssl_error else ssl._create_unverified_context()
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                raw = r.read(max_bytes)
                enc = r.headers.get_content_charset() or "utf-8"
                _blocks[dom] = 0
                return {"status": r.status, "url": r.geturl(), "text": raw.decode(enc, "replace"),
                        "error": "", "ssl_error": ssl_error}
        except urllib.error.HTTPError as e:
            text = ""
            try:
                text = e.read(200_000).decode("utf-8", "replace")
            except Exception:
                pass
            if e.code in (403, 429):
                _blocks[dom] = _blocks.get(dom, 0) + 1
                if attempt < pol["max_retries_on_block"] and _blocks[dom] < pol["stop_source_after_blocks"]:
                    attempt += 1
                    ra = e.headers.get("Retry-After") if e.headers else None
                    time.sleep(int(ra) if ra and ra.isdigit() else pol["backoff_base_s"] * attempt)
                    continue
            return {"status": e.code, "url": url, "text": text, "error": f"HTTP {e.code}", "ssl_error": ssl_error}
        except (ssl.SSLError, urllib.error.URLError) as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, ssl.SSLError) or isinstance(e, ssl.SSLError) or "CERTIFICATE" in str(e).upper():
                if not ssl_error:
                    # Retry once without verification only to *observe* the page; the SSL problem is recorded.
                    ssl_error = f"SSL: {reason}"
                    continue
            return {"status": None, "url": url, "text": "", "error": f"{type(reason).__name__}: {reason}",
                    "ssl_error": ssl_error}
        except Exception as e:
            return {"status": None, "url": url, "text": "", "error": f"{type(e).__name__}: {e}", "ssl_error": ssl_error}


# ---------------------------------------------------------------- Apify (paid; hard spend caps)
def env_secret(name):
    val = os.environ.get(name)
    if not val:
        try:  # `setx` values are not visible to already-open shells; read the user registry.
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                val = winreg.QueryValueEx(k, name)[0]
        except Exception:
            val = None
    return val


def week_spend():
    path = os.path.join(data_dir(), "costs.csv")
    if not os.path.exists(path):
        return 0.0
    y, w, _ = now().isocalendar()
    total = 0.0
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            d = parse_date(r.get("at"))
            if d and d.isocalendar()[:2] == (y, w):
                total += float(r.get("usd") or 0)
    return total


def check_budget(usd):
    lim = cfg()["limits"]
    usd = max(0.5, usd)
    if usd > lim["max_usd_per_run"]:
        sys.exit(f"Requested cap {usd} USD exceeds max_usd_per_run ({lim['max_usd_per_run']}) in config.json.")
    spent = week_spend()
    if spent + usd > lim["max_usd_per_week"]:
        sys.exit(f"Weekly cap: spent {spent:.2f} + {usd} USD > {lim['max_usd_per_week']} USD (config.json).")


def _apify_api(method, url, token, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def apify_run(actor, actor_input, max_usd, run_id, label, max_wait_s=1800):
    """Run an Apify actor with a hard spend cap. Returns (items, usd, status). Raw output saved under runs/."""
    token = env_secret("APIFY_TOKEN") or env_secret("APIFY_API_TOKEN")
    if not token:
        raise RuntimeError("APIFY_TOKEN not set (PowerShell: setx APIFY_TOKEN \"<token>\")")
    check_budget(max_usd)
    base = "https://api.apify.com/v2"
    q = urllib.parse.urlencode({"maxTotalChargeUsd": max(0.5, max_usd)})
    try:
        run = _apify_api("POST", f"{base}/acts/{actor}/runs?{q}", token, actor_input)["data"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Apify HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
    rid = run["id"]
    print(f"[apify] {label}: run {rid} started (cap {max(0.5, max_usd)} USD)…", flush=True)
    t0 = time.time()
    while run["status"] in ("READY", "RUNNING"):
        if time.time() - t0 > max_wait_s:
            raise RuntimeError(f"Apify {label}: timeout, run {rid} still running")
        time.sleep(5)
        run = _apify_api("GET", f"{base}/actor-runs/{rid}", token)["data"]
    usd = float(run.get("usageTotalUsd") or 0)
    items = _apify_api("GET", f"{base}/datasets/{run['defaultDatasetId']}/items?clean=true&format=json",
                       token, timeout=120)
    d = data_dir("runs", run_id)
    with open(os.path.join(d, f"{label}_raw.json"), "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    with open(os.path.join(d, f"{label}_meta.json"), "w", encoding="utf-8") as f:
        json.dump({"actor": actor, "input": actor_input, "apify_run_id": rid, "status": run["status"],
                   "usd": usd, "items": len(items), "at": now_iso()}, f, ensure_ascii=False, indent=1)
    path = os.path.join(data_dir(), "costs.csv")
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["run_id", "label", "usd", "at", "note"])
        w.writerow([run_id, label, f"{usd:.4f}", now_iso(), f"{actor} {run['status']} items={len(items)}"])
    print(f"[apify] {label}: {run['status']}, {len(items)} items, {usd:.4f} USD", flush=True)
    return items, usd, run["status"]


def ad_library_url(name=None, page_id=None):
    base = cfg()["sources"]["ad_library"]
    if page_id:
        return f"{base}&view_all_page_id={page_id}&search_type=page"
    return f"{base}&q={urllib.parse.quote(name or '')}&search_type=keyword_unordered"

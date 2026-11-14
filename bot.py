"""
mymarket.ge -> Telegram notifier (v3)

  python bot.py                 one check (used by GitHub Actions / Task Scheduler)
  python bot.py --loop          check forever, every CHECK_INTERVAL seconds (default 300)
  python bot.py --loop 180      ... with another interval
  python bot.py --dry-run       show what every watch would match right now; sends nothing, saves nothing
  python bot.py --selftest      check every external piece (Telegram, mymarket, photos, details, rate, AI)
  python bot.py --resolve URL   turn a mymarket.ge URL into a "payload" for watches.json
  python bot.py --reset         forget everything seen so far

Files (always next to this script):
  .env          TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (+ optional settings, see README)
  watches.json  what to watch and how to filter
  state.json    memory (created automatically; an old seen.json is imported once)
  bot.log       log file
"""

import argparse
import hashlib
import html
import json
import logging
import os
import re
import socket
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import requests
from curl_cffi import requests as cf_requests
from dotenv import load_dotenv

# --------------------------------------------------------------------------- config

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env", encoding="utf-8-sig")


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
CHAT_ID = _env("TELEGRAM_CHAT_ID")

API_URL = "https://api.mymarket.ge/api/ka/products"
NBG_URL = "https://nbg.gov.ge/gw/api/ct/monetarypolicy/currencies/en/json/?currencies=USD"

WATCHES_FILE = BASE / "watches.json"
STATE_FILE = BASE / "state.json"
OLD_SEEN_FILE = BASE / "seen.json"
LOG_FILE = BASE / "bot.log"

CHECK_INTERVAL = int(_env("CHECK_INTERVAL", "300"))            # seconds between checks in --loop
FIRST_RUN_LIMIT = int(_env("FIRST_RUN_LIMIT", "3"))            # previews sent for a new/changed watch
USD_RATE_FALLBACK = float(_env("USD_RATE", "2.7"))             # only if the NBG rate can't be fetched
SEND_PHOTOS = _env("SEND_PHOTOS", "1") != "0"
PRICE_DROP_MIN_PCT = float(_env("PRICE_DROP_MIN_PCT", "3"))    # notify when price falls by >= this %
RENEWED_ID_GAP = int(_env("RENEWED_ID_GAP", "150000"))         # ID this far below the newest = old listing bumped up
KEEP_DAYS = int(_env("KEEP_DAYS", "60"))                       # forget listings not seen for this many days
ALERT_AFTER_FAILS = int(_env("ALERT_AFTER_FAILS", "3"))        # failed checks in a row before alerting you

# optional AI filter ("ai_rule" in a watch). Set ONE of these keys in .env
GEMINI_API_KEY = _env("GEMINI_API_KEY")
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
AI_MODEL = _env("AI_MODEL")                                    # empty = provider default below
AI_MAX_PER_RUN = int(_env("AI_MAX_PER_RUN", "25"))             # safety cap on AI calls per check
GEMINI_DEFAULT_MODEL = "gemini-3.6-flash"
ANTHROPIC_DEFAULT_MODEL = "claude-haiku-5-5"

CURRENCY_GEL = 3
CURRENCY_USD = 1
LOCK_PORT = 47653
DAY = 86400

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://mymarket.ge",
    "Referer": "https://mymarket.ge/",
}

# candidates, probed automatically; the one that works is remembered in state.json
PHOTO_TEMPLATES = [
    "https://static.my.ge/mymarket/photos/{photo}/large/{id}_1.jpg?v={ver}",
    "https://static.my.ge/mymarket/photos/{photo}/thumbs/{id}_1.jpg?v={ver}",
    "https://static.my.ge/mymarket/photos/large/{photo}/{id}_1.jpg?v={ver}",
]
DETAIL_VARIANTS = [
    ("GET", "https://api.mymarket.ge/api/ka/products/{id}"),
    ("POST", "https://api.mymarket.ge/api/ka/products/{id}"),
    ("GET", "https://api.mymarket.ge/api/ka/product/{id}"),
]

log = logging.getLogger("bot")


def setup_logging(to_file: bool = True) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    log.setLevel(logging.INFO)
    con_h = logging.StreamHandler(sys.stdout)
    con_h.setFormatter(fmt)
    log.addHandler(con_h)
    if to_file:
        file_h = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        file_h.setFormatter(fmt)
        log.addHandler(file_h)


# --------------------------------------------------------------------------- state

def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_state() -> dict:
    state = load_json(STATE_FILE) if STATE_FILE.exists() else {}
    for key, default in (("watches", {}), ("max_id", 0), ("fail_count", 0), ("alerted", False),
                         ("photo_tpl", None), ("photo_ts", 0), ("photo_fails", 0),
                         ("detail_variant", None), ("detail_ts", 0)):
        state.setdefault(key, default)
    return state


_last_saved = {"data": None}


def save_state(state: dict) -> None:
    """Atomic write, skipped when nothing changed; retries because OneDrive/antivirus may lock the file."""
    data = json.dumps(state, ensure_ascii=False, sort_keys=True)
    if data == _last_saved["data"] and STATE_FILE.exists():
        return
    tmp = STATE_FILE.with_suffix(".tmp")
    for attempt in range(5):
        try:
            tmp.write_text(data, encoding="utf-8")
            os.replace(tmp, STATE_FILE)
            _last_saved["data"] = data
            return
        except PermissionError:
            time.sleep(0.5 * (attempt + 1))
    STATE_FILE.write_text(data, encoding="utf-8")
    _last_saved["data"] = data


def watch_fingerprint(watch: dict) -> str:
    relevant = {k: v for k, v in watch.items() if k not in ("name", "pages", "enabled")}
    return hashlib.sha1(json.dumps(relevant, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


def import_old_seen() -> dict:
    """Old seen.json (plain list of IDs) -> items dict, so nothing is re-sent after upgrading."""
    if not OLD_SEEN_FILE.exists():
        return {}
    try:
        now = int(time.time())
        return {str(i): {"p": None, "c": None, "t": now} for i in load_json(OLD_SEEN_FILE)}
    except Exception as e:
        log.warning("Could not import seen.json: %s", e)
        return {}


# --------------------------------------------------------------------------- prices

_rate_cache = {"rate": None, "ts": 0.0}


def usd_rate() -> float:
    """GEL per 1 USD from the National Bank of Georgia, cached for 6 hours."""
    if _rate_cache["rate"] and time.time() - _rate_cache["ts"] < 6 * 3600:
        return _rate_cache["rate"]
    try:
        r = requests.get(NBG_URL, timeout=10)
        r.raise_for_status()
        cur = r.json()[0]["currencies"][0]
        rate = float(cur["rate"]) / float(cur.get("quantity") or 1)
        if not 1 < rate < 10:
            raise ValueError(f"suspicious rate {rate}")
        _rate_cache.update(rate=rate, ts=time.time())
        return rate
    except Exception as e:
        fallback = _rate_cache["rate"] or USD_RATE_FALLBACK
        log.warning("NBG rate unavailable (%s), using %s", type(e).__name__, fallback)
        _rate_cache.update(rate=fallback, ts=time.time() - 5 * 3600)   # retry in an hour
        return fallback


def item_price(item: dict) -> tuple[float, int]:
    try:
        price = float(item.get("price") or 0)
    except (TypeError, ValueError):
        price = 0.0
    return price, item.get("currency_id")


def price_in_gel(price: float, currency_id) -> float:
    return price * usd_rate() if currency_id == CURRENCY_USD else price


def fmt_num(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ")


def fmt_price(price: float, currency_id) -> str:
    if price <= 0:
        return "ფასი შეთანხმებით"
    if currency_id == CURRENCY_USD:
        return f"{fmt_num(price)} $ (≈ {fmt_num(price * usd_rate())} ₾)"
    if currency_id == CURRENCY_GEL:
        return f"{fmt_num(price)} ₾ (≈ {fmt_num(price / usd_rate())} $)"
    return fmt_num(price)


# --------------------------------------------------------------------------- keyword filters

def _kw_regex(keyword: str) -> re.Pattern:
    """
    Substring match, but a Latin letter/digit edge must not touch another Latin letter/digit.
    "8GB" does not match "48GB"/"18GB", "M3" does not match "M30",
    while Georgian words still match with any suffix ("დაზიანებული" matches "დაზიანებულია").
    """
    kw = keyword.strip().lower()
    pat = re.escape(kw)
    if kw and kw[0].isascii() and kw[0].isalnum():
        pat = r"(?<![a-z0-9])" + pat
    if kw and kw[-1].isascii() and kw[-1].isalnum():
        pat = pat + r"(?![a-z0-9])"
    return re.compile(pat)


def matches_watch(item: dict, watch: dict, descr: str | None = None) -> bool:
    """Cheap filters: price (in GEL) and keywords."""
    price, cur = item_price(item)
    min_p, max_p = watch.get("min_price"), watch.get("max_price")

    if price <= 0:
        if (min_p is not None or max_p is not None) and not watch.get("allow_no_price", True):
            return False
    else:
        gel = price_in_gel(price, cur)
        if min_p is not None and gel < min_p:
            return False
        if max_p is not None and gel > max_p:
            return False

    blob = f"{item.get('title') or ''} {descr if descr is not None else (item.get('descr') or '')}".lower()

    must_any = [k for k in watch.get("must_include_any", []) if k.strip()]
    if must_any and not any(_kw_regex(k).search(blob) for k in must_any):
        return False
    must_all = [k for k in watch.get("must_include_all", []) if k.strip()]
    if any(not _kw_regex(k).search(blob) for k in must_all):
        return False
    exclude = [k for k in watch.get("exclude", []) if k.strip()]
    if any(_kw_regex(k).search(blob) for k in exclude):
        return False
    return True


# --------------------------------------------------------------------------- mymarket

def fetch_page(payload: dict) -> list[dict]:
    last_err = None
    for attempt in range(3):
        try:
            r = cf_requests.post(API_URL, json=payload, headers=HEADERS, impersonate="chrome", timeout=20)
            if r.status_code != 200:
                raise RuntimeError(f"mymarket HTTP {r.status_code}")
            body = r.json()
            if body.get("statusCode") not in (200, None):
                raise RuntimeError(f"mymarket API {body.get('statusCode')}: {body.get('statusMessage')}")
            data = body.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("Prs"), list):
                raise RuntimeError("mymarket response has no data.Prs (API changed?)")
            return data["Prs"]
        except Exception as e:
            last_err = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"fetch failed after 3 tries: {last_err}")


def fetch_watch(watch: dict, payload: dict) -> list[dict]:
    """All items of the watched pages, newest first, without duplicates."""
    items, ids = [], set()
    for page in range(1, int(watch.get("pages", 2)) + 1):
        p = dict(payload)
        p["Page"] = page
        prs = fetch_page(p)
        if page == 1 and not prs:
            raise RuntimeError("page 1 is empty (blocked, or wrong payload?)")
        for it in prs:
            pid = it.get("product_id")
            if pid and pid not in ids:
                ids.add(pid)
                items.append(it)
        if not prs:
            break
        time.sleep(1)
    return items


def _find_key(obj, key: str):
    """First non-empty string stored under `key` anywhere inside nested JSON."""
    if isinstance(obj, dict):
        v = obj.get(key)
        if isinstance(v, str) and v.strip():
            return v
        for child in obj.values():
            found = _find_key(child, key)
            if found:
                return found
    elif isinstance(obj, list):
        for child in obj:
            found = _find_key(child, key)
            if found:
                return found
    return None


def fetch_detail_descr(state: dict, pid: int) -> str | None:
    """
    Full description from the listing's own endpoint. The endpoint is auto-detected among
    DETAIL_VARIANTS and remembered; if none works the bot keeps using the list description.
    """
    v = state["detail_variant"]
    if v == -1 and time.time() - state["detail_ts"] < 7 * DAY:
        return None
    order = [v] if isinstance(v, int) and v >= 0 else range(len(DETAIL_VARIANTS))
    for i in order:
        method, tpl = DETAIL_VARIANTS[i]
        try:
            r = cf_requests.request(method, tpl.format(id=pid), headers=HEADERS,
                                    impersonate="chrome", timeout=20)
            if r.status_code != 200:
                continue
            body = r.json()
            if body.get("statusCode") not in (200, None):
                continue
            descr = _find_key(body.get("data"), "descr")
            if descr:
                if state["detail_variant"] != i:
                    log.info("Detail endpoint detected: %s %s", method, tpl)
                state["detail_variant"] = i
                return descr
        except Exception:
            continue
    if not (isinstance(v, int) and v >= 0):
        state["detail_variant"], state["detail_ts"] = -1, int(time.time())
        log.warning("No working detail endpoint found; using list descriptions (retry in 7 days)")
    return None


# --------------------------------------------------------------------------- URL -> payload

def _num(v: str):
    return int(v) if re.fullmatch(r"-?\d+", v or "") else v


def payload_from_url(url: str) -> dict:
    """
    Best effort: turn a mymarket.ge listing-page URL into an API payload.
    1) query parameters (?CatID=53&Brands=42&PriceTo=...) are used as they are
    2) /search/<number>/ in the path is the CatID
    3) otherwise the page HTML is searched for CatID / Brands
    Raises RuntimeError when nothing usable is found.
    """
    u = urlparse(url)
    payload = {k: _num(v) for k, v in parse_qsl(u.query) if v != "" and k.lower() != "page"}
    if "CatID" not in payload:
        m = re.search(r"/search/(\d+)(?:/|$)", u.path)
        if m:
            payload["CatID"] = int(m.group(1))
    if "CatID" not in payload:
        r = cf_requests.get(url, impersonate="chrome", timeout=25,
                            headers={"Accept": "text/html,application/xhtml+xml"})
        if r.status_code != 200:
            raise RuntimeError(f"could not open the URL (HTTP {r.status_code})")
        page = r.text
        for key, pats in (("CatID", (r'"CatID"\s*:\s*"?(\d+)', r'"cat_id"\s*:\s*"?(\d+)', r'"catId"\s*:\s*"?(\d+)',
                                     r'CatID=(\d+)')),
                          ("Brands", (r'"Brands"\s*:\s*"?(\d+)', r'"brand_id"\s*:\s*"?(\d+)', r'Brands=(\d+)'))):
            if key in payload:
                continue
            for pat in pats:
                m = re.search(pat, page)
                if m:
                    payload[key] = int(m.group(1))
                    break
    if "CatID" not in payload:
        raise RuntimeError("could not find a CatID for this URL - put a \"payload\" in watches.json instead "
                           "(Chrome F12 > Network > Fetch/XHR > 'products' request > Payload)")
    payload.setdefault("SortID", 1)
    return payload


def watch_payload(watch: dict, ws: dict) -> dict:
    if watch.get("payload"):
        return dict(watch["payload"])
    if not watch.get("url"):
        raise RuntimeError("watch needs a \"payload\" or a \"url\"")
    if ws.get("url") != watch["url"] or not ws.get("payload"):
        ws["payload"] = payload_from_url(watch["url"])
        ws["url"] = watch["url"]
        log.info("Watch '%s': URL resolved to payload %s", watch["name"], ws["payload"])
    return dict(ws["payload"])


# --------------------------------------------------------------------------- AI filter

_ai = {"calls": 0, "warned": False, "gemini_model": None}


def ai_provider() -> str | None:
    if GEMINI_API_KEY:
        return "gemini"
    if ANTHROPIC_API_KEY:
        return "anthropic"
    return None


def _ai_prompt(rule: str, item: dict, descr: str) -> str:
    price, cur = item_price(item)
    return (
        "You filter marketplace listings from mymarket.ge (Georgia) for a buyer. "
        "Listings are usually in Georgian, sometimes English or Russian.\n\n"
        f"BUYER'S RULE:\n{rule}\n\n"
        "LISTING:\n"
        f"Title: {item.get('title') or ''}\n"
        f"Price: {fmt_price(price, cur)}\n"
        f"Seller: {item.get('user') or ''}\n"
        f"Description: {descr[:3000]}\n\n"
        "Does this listing satisfy the buyer's rule? If the listing does not give enough information "
        "to break the rule, treat it as a match.\n"
        'Answer with JSON only: {"match": true or false, "reason": "max 12 words, in Georgian"}'
    )


def _gemini_generate(model: str, prompt: str) -> requests.Response:
    return requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"responseMimeType": "application/json"}},
        timeout=40,
    )


def _gemini_pick_model() -> str | None:
    """When the configured model does not exist any more: take a current Flash model from the API's own list."""
    r = requests.get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                     headers={"x-goog-api-key": GEMINI_API_KEY}, timeout=20)
    r.raise_for_status()
    names = [m["name"].split("/", 1)[-1] for m in r.json().get("models", [])
             if "generateContent" in m.get("supportedGenerationMethods", [])]
    plain = [n for n in names if "flash" in n and not any(x in n for x in ("image", "tts", "audio", "live", "preview", "exp"))]
    return (sorted(plain, reverse=True) or sorted([n for n in names if "flash" in n], reverse=True) or [None])[0]


def _ai_raw(prompt: str) -> str:
    if ai_provider() == "gemini":
        model = _ai["gemini_model"] or AI_MODEL or GEMINI_DEFAULT_MODEL
        r = _gemini_generate(model, prompt)
        if r.status_code == 404:
            picked = _gemini_pick_model()
            if not picked:
                raise RuntimeError("no usable Gemini model found")
            log.warning("Gemini model '%s' not found, using '%s'", model, picked)
            _ai["gemini_model"] = picked
            r = _gemini_generate(picked, prompt)
        if r.status_code != 200:
            raise RuntimeError(f"Gemini HTTP {r.status_code}")
        parts = r.json()["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": AI_MODEL or ANTHROPIC_DEFAULT_MODEL, "max_tokens": 200,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=40,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Anthropic HTTP {r.status_code}")
    return "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")


def ai_check(rule: str, item: dict, descr: str) -> tuple[bool | None, str]:
    """
    (True/False, reason) from the model, or (None, why) when the AI could not be asked.
    None is treated as "send it anyway" so a broken key or a quota never hides a listing.
    """
    if not ai_provider():
        if not _ai["warned"]:
            log.warning("A watch has an ai_rule but no GEMINI_API_KEY / ANTHROPIC_API_KEY is set; rule ignored")
            _ai["warned"] = True
        return None, ""
    if _ai["calls"] >= AI_MAX_PER_RUN:
        return None, "AI ლიმიტი"
    _ai["calls"] += 1
    try:
        raw = _ai_raw(_ai_prompt(rule, item, descr))
        m = re.search(r"\{.*\}", raw, re.S)
        verdict = json.loads(m.group(0) if m else raw)
        return bool(verdict.get("match")), str(verdict.get("reason") or "")[:120]
    except Exception as e:
        log.warning("AI check failed (%s); sending the listing unfiltered", e)
        return None, "AI შემოწმება ვერ მოხერხდა"


# --------------------------------------------------------------------------- telegram

def tg_call(method: str, payload: dict) -> dict:
    """Telegram API call with rate-limit handling. Raises RuntimeError on failure."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    for attempt in range(4):
        try:
            resp = requests.post(url, json=payload, timeout=20)
        except requests.RequestException as e:
            if attempt == 3:
                raise RuntimeError(f"Telegram unreachable: {type(e).__name__}") from None
            time.sleep(3 * (attempt + 1))
            continue
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code == 200 and body.get("ok"):
            return body
        if resp.status_code == 429:
            wait = int(body.get("parameters", {}).get("retry_after", 5)) + 1
            log.warning("Telegram rate limit, waiting %ss", wait)
            time.sleep(wait)
            continue
        if resp.status_code >= 500 and attempt < 3:
            time.sleep(3 * (attempt + 1))
            continue
        raise RuntimeError(f"Telegram {method} {resp.status_code}: {body.get('description')}")
    raise RuntimeError(f"Telegram {method}: still rate-limited after retries")


def _photo_fmt(tpl: str, item: dict) -> str:
    return tpl.format(photo=item["photo"], id=item["product_id"], ver=item.get("photo_ver") or 0)


def probe_photo(item: dict) -> str:
    """Which URL pattern really serves this listing's first photo? '' if none."""
    for tpl in PHOTO_TEMPLATES:
        try:
            r = cf_requests.get(_photo_fmt(tpl, item), impersonate="chrome", timeout=15)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                return tpl
        except Exception:
            continue
    return ""


def photo_url(state: dict, item: dict) -> str | None:
    if not SEND_PHOTOS or not item.get("photo") or not item.get("photos_cnt"):
        return None
    tpl = state["photo_tpl"]
    if tpl is None or (tpl == "" and time.time() - state["photo_ts"] > DAY):
        tpl = probe_photo(item)
        state["photo_tpl"], state["photo_ts"], state["photo_fails"] = tpl, int(time.time()), 0
        log.info("Photo URL pattern: %s", tpl or "none found - sending text messages (retry tomorrow)")
    return _photo_fmt(tpl, item) if tpl else None


def send_listing(state: dict, text: str, link: str, item: dict) -> None:
    keyboard = {"inline_keyboard": [[{"text": "გახსნა mymarket-ზე", "url": link}]]}
    url = photo_url(state, item)
    if url and len(text) <= 1024:
        try:
            tg_call("sendPhoto", {"chat_id": CHAT_ID, "photo": url, "caption": text,
                                  "parse_mode": "HTML", "reply_markup": keyboard})
            state["photo_fails"] = 0
            return
        except RuntimeError as e:
            state["photo_fails"] += 1
            log.info("Photo not sent (%s), sending as text", e)
            if state["photo_fails"] >= 3:      # pattern stopped working: probe again next time
                state["photo_tpl"], state["photo_fails"] = None, 0
    tg_call("sendMessage", {"chat_id": CHAT_ID, "text": f'{text}\n\n<a href="{link}">{link}</a>',
                            "parse_mode": "HTML", "reply_markup": keyboard})


def send_plain(text: str) -> None:
    tg_call("sendMessage", {"chat_id": CHAT_ID, "text": text[:3900], "disable_web_page_preview": True})


def build_message(watch: dict, item: dict, kind: str, old: dict | None, descr: str, ai_reason: str) -> tuple[str, str]:
    pid = item["product_id"]
    link = f"https://www.mymarket.ge/ka/pr/{pid}"
    price, cur = item_price(item)
    esc = html.escape

    head = {"new": "🆕 ახალი", "renewed": "♻️ განახლებული (ძველი განცხადება)", "drop": "📉 ფასი დაიკლო"}[kind]
    if (item.get("vip") or 0) > 0:
        head += " · ⭐ VIP"

    lines = [f"{head} · <i>{esc(watch['name'])}</i>",
             f"<b>{esc((item.get('title') or 'უსათაურო').strip())}</b>"]

    if kind == "drop" and old:
        pct = (old["p"] - price) / old["p"] * 100
        lines.append(f"💰 <s>{fmt_num(old['p'])}</s> → <b>{esc(fmt_price(price, cur))}</b>  (−{pct:.0f}%)")
    else:
        lines.append(f"💰 <b>{esc(fmt_price(price, cur))}</b>")

    meta = []
    if item.get("user"):
        meta.append(f"👤 {esc(str(item['user']))}")
    if item.get("views") is not None:
        meta.append(f"👁 {item['views']}")
    if item.get("order_date"):
        meta.append(f"🕒 {esc(str(item['order_date'])[:16])}")
    if meta:
        lines.append(" · ".join(meta))
    if ai_reason:
        lines.append(f"🤖 {esc(ai_reason)}")

    descr = " ".join((descr or "").split())
    if len(descr) > 300:
        descr = descr[:297] + "..."
    if descr:
        lines.append("")
        lines.append(esc(descr))
    return "\n".join(lines), link


# --------------------------------------------------------------------------- core

def evaluate(item: dict, watch: dict, state: dict) -> tuple[bool, bool | None, str, str]:
    """
    Full check of one listing: cheap filters -> (full description) -> cheap filters again -> AI rule.
    Returns (send_it, ai_verdict, ai_reason, description_to_show).
    """
    descr = item.get("descr") or ""
    if watch.get("fetch_details", False):
        full = fetch_detail_descr(state, item["product_id"])
        if full and len(full) >= len(descr):
            descr = full
    if not matches_watch(item, watch, descr):
        return False, None, "", descr
    rule = (watch.get("ai_rule") or "").strip()
    if not rule:
        return True, None, "", descr
    verdict, reason = ai_check(rule, item, descr)
    return verdict is not False, verdict, reason, descr


def process_watch(watch: dict, state: dict, max_id_before: int) -> int:
    name = watch["name"]
    fp = watch_fingerprint(watch)
    ws = state["watches"].get(name)

    baseline = False
    if ws is None:
        ws = {"fp": fp, "items": import_old_seen() if not state["watches"] else {}}
        state["watches"][name] = ws
        baseline = True
        log.info("Watch '%s': first run, sending up to %d previews", name, FIRST_RUN_LIMIT)
    elif ws.get("fp") != fp:
        ws["fp"] = fp
        baseline = True
        log.info("Watch '%s': filters changed, sending up to %d previews", name, FIRST_RUN_LIMIT)

    items = fetch_watch(watch, watch_payload(watch, ws))
    known = ws["items"]
    now = int(time.time())
    sent = 0

    for item in items:
        pid = item["product_id"]
        key = str(pid)
        price, cur = item_price(item)
        prev = known.get(key)
        is_vip = (item.get("vip") or 0) > 0

        kind, ai_verdict, ai_reason, descr = None, (prev or {}).get("ai"), "", item.get("descr") or ""

        if baseline:
            # preview the newest matching non-VIP listings, remember everything else silently
            if sent < FIRST_RUN_LIMIT and not is_vip and matches_watch(item, watch):
                ok, ai_verdict, ai_reason, descr = evaluate(item, watch, state)
                kind = "new" if ok else None
        elif prev is None:
            if matches_watch(item, watch) or watch.get("fetch_details", False):
                ok, ai_verdict, ai_reason, descr = evaluate(item, watch, state)
                if ok:
                    is_old = max_id_before and pid < max_id_before - RENEWED_ID_GAP
                    kind = "renewed" if is_old else "new"
                    if kind == "renewed" and watch.get("skip_renewed", False):
                        kind = None
        elif (watch.get("price_drops", True) and prev.get("ai") is not False
              and prev.get("p") and price > 0 and prev.get("c") == cur
              and price <= prev["p"] * (1 - PRICE_DROP_MIN_PCT / 100)
              and matches_watch(item, watch)):
            kind = "drop"

        if kind:
            text, link = build_message(watch, item, kind, prev, descr, ai_reason)
            send_listing(state, text, link, item)     # raises if Telegram fails -> listing stays "unseen"
            sent += 1
            log.info("Sent [%s] %s: %s - %s", name, kind, pid, (item.get("title") or "")[:60])
            time.sleep(0.4)

        # remember it (only reached when sending succeeded or nothing had to be sent)
        entry = {"p": price if price > 0 else (prev or {}).get("p"),
                 "c": cur if price > 0 else (prev or {}).get("c"),
                 # refresh the timestamp only twice a day so state.json rarely changes on quiet checks
                 "t": prev["t"] if prev and now - prev.get("t", 0) < DAY // 2 else now}
        if ai_verdict is not None:
            entry["ai"] = ai_verdict
        known[key] = entry
        state["max_id"] = max(state["max_id"], pid)
        if kind:
            save_state(state)                         # a crash right after never causes a duplicate

    cutoff = now - KEEP_DAYS * DAY
    for k in [k for k, v in known.items() if v.get("t", now) < cutoff]:
        del known[k]

    save_state(state)
    return sent


def load_watches() -> list[dict]:
    watches = load_json(WATCHES_FILE)
    names = [w.get("name") for w in watches]
    if any(not n for n in names) or len(names) != len(set(names)):
        raise RuntimeError("watches.json: every watch needs a unique \"name\"")
    return watches


def run_once(state: dict) -> int:
    watches = load_watches()
    _ai["calls"] = 0
    max_id_before = state["max_id"]
    total, errors = 0, []
    for watch in watches:
        if not watch.get("enabled", True):
            continue
        try:
            total += process_watch(watch, state, max_id_before)
        except Exception as e:                        # one broken watch must not stop the others
            errors.append(f"[{watch['name']}] {e}")
            log.error("Watch '%s' failed: %s", watch["name"], e)

    names = {w["name"] for w in watches}
    for gone in [n for n in state["watches"] if n not in names]:
        del state["watches"][gone]
    save_state(state)
    if errors:
        raise RuntimeError("; ".join(errors))
    return total


def guarded_run(state: dict) -> bool:
    """One check with failure tracking and Telegram alerts. Returns True on success."""
    try:
        sent = run_once(state)
        if state["alerted"]:
            try:
                send_plain("✅ mymarket bot: ისევ მუშაობს.")
            except Exception:
                pass
        state["fail_count"], state["alerted"] = 0, False
        save_state(state)
        log.info("Check done, %d message(s) sent", sent)
        return True
    except Exception as e:
        state["fail_count"] += 1
        log.error("Check failed (%d in a row): %s", state["fail_count"], e)
        if state["fail_count"] >= ALERT_AFTER_FAILS and not state["alerted"]:
            try:
                send_plain(f"⚠️ mymarket bot: {state['fail_count']} შემოწმება ზედიზედ ჩავარდა.\n{e}")
                state["alerted"] = True
            except Exception as e2:
                log.error("Could not send alert: %s", e2)
        try:
            save_state(state)
        except Exception:
            pass
        return False


# --------------------------------------------------------------------------- tools

def cmd_dry_run() -> None:
    """Show, for every enabled watch, what is on its pages right now and what the cheap filters keep."""
    state = load_state()
    for watch in load_watches():
        if not watch.get("enabled", True):
            continue
        print(f"\n=== {watch['name']} ===")
        try:
            ws = dict(state["watches"].get(watch["name"]) or {})
            items = fetch_watch(watch, watch_payload(watch, ws))
        except Exception as e:
            print(f"  FAILED: {e}")
            continue
        kept = 0
        for it in items:
            ok = matches_watch(it, watch)
            kept += ok
            price, cur = item_price(it)
            vip = " VIP" if (it.get("vip") or 0) > 0 else ""
            print(f"  {'✓' if ok else '✗'} {it['product_id']}{vip} | {fmt_price(price, cur)} | {(it.get('title') or '')[:70]}")
        extra = "  (ai_rule is applied on top of this for new listings)" if watch.get("ai_rule") else ""
        print(f"  -> {kept} of {len(items)} pass price/keyword filters{extra}")


def cmd_selftest() -> None:
    """Checks every external dependency and prints a report you can paste back if something is off."""
    state = load_state()
    ok_all = True

    def report(label: str, ok: bool, info: str = "") -> None:
        nonlocal ok_all
        ok_all = ok_all and ok
        print(f"[{'OK  ' if ok else 'FAIL'}] {label}{': ' + info if info else ''}")

    def optional(label: str, ok: bool, info: str = "") -> None:
        print(f"[{'OK  ' if ok else 'SKIP'}] {label}{': ' + info if info else ''}")

    report(".env", bool(BOT_TOKEN and CHAT_ID), "token and chat id found" if BOT_TOKEN and CHAT_ID else "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing")
    try:
        me = tg_call("getMe", {})["result"]
        send_plain("✅ mymarket bot selftest: Telegram მუშაობს.")
        report("Telegram", True, f"@{me.get('username')} can message chat {CHAT_ID}")
    except Exception as e:
        report("Telegram", False, str(e))

    sample = None
    try:
        watches = load_watches()
        report("watches.json", True, f"{len(watches)} watch(es)")
    except Exception as e:
        watches = []
        report("watches.json", False, str(e))
    for w in watches:
        if not w.get("enabled", True):
            continue
        try:
            items = fetch_watch({**w, "pages": 1}, watch_payload(w, {}))
            sample = sample or next((i for i in items if i.get("photo") and i.get("photos_cnt")), items[0])
            report(f"mymarket API [{w['name']}]", True, f"{len(items)} listings on page 1, {sum(matches_watch(i, w) for i in items)} pass filters")
        except Exception as e:
            report(f"mymarket API [{w['name']}]", False, str(e))

    try:
        _rate_cache["rate"] = None
        r = requests.get(NBG_URL, timeout=10)
        r.raise_for_status()
        report("NBG exchange rate", True, f"1 $ = {r.json()[0]['currencies'][0]['rate']} ₾")
    except Exception as e:
        optional("NBG exchange rate", False, f"{type(e).__name__}; fallback {USD_RATE_FALLBACK} is used")

    if sample:
        tpl = probe_photo(sample)
        state["photo_tpl"], state["photo_ts"] = tpl, int(time.time())
        optional("Listing photos", bool(tpl), tpl or "no known URL pattern works; messages are sent as text with a link preview")
        state["detail_variant"] = None
        full = fetch_detail_descr(state, sample["product_id"])
        v = state["detail_variant"]
        optional("Detail endpoint (fetch_details)", bool(full),
                 f"{DETAIL_VARIANTS[v][0]} {DETAIL_VARIANTS[v][1]} ({len(full)} chars vs {len(sample.get('descr') or '')} in list)"
                 if full else "not found; \"fetch_details\" has no effect, list descriptions are used")
        save_state(state)

    if ai_provider():
        verdict, reason = ai_check("Any laptop is fine.", sample or {"title": "MacBook Air M3", "price": "3000", "currency_id": 3}, (sample or {}).get("descr") or "")
        model = _ai["gemini_model"] or AI_MODEL or (GEMINI_DEFAULT_MODEL if ai_provider() == "gemini" else ANTHROPIC_DEFAULT_MODEL)
        report(f"AI filter ({ai_provider()}, {model})", verdict is not None, f"answered: match={verdict}, {reason}" if verdict is not None else "no answer - check the key / AI_MODEL (see bot.log)")
    else:
        optional("AI filter", False, "no GEMINI_API_KEY / ANTHROPIC_API_KEY in .env; \"ai_rule\" is ignored")

    print("\nAll required checks passed." if ok_all else "\nSome required checks FAILED - see above.")


def cmd_resolve(url: str) -> None:
    try:
        payload = payload_from_url(url)
    except Exception as e:
        print(f"FAILED: {e}")
        return
    print("Payload:\n  " + json.dumps(payload, ensure_ascii=False))
    try:
        items = fetch_page({**payload, "Page": 1})
        print(f"The API returns {len(items)} listings for it. First ones:")
        for it in items[:5]:
            print(f"  - {(it.get('title') or '')[:80]}")
        print("If these are the listings you expect, copy the payload into watches.json.")
    except Exception as e:
        print(f"But the API call with it failed: {e}")


def acquire_lock() -> socket.socket | None:
    """Only one --loop instance per machine (otherwise every listing would arrive twice)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        return s
    except OSError:
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description="mymarket.ge -> Telegram notifier")
    ap.add_argument("--loop", nargs="?", type=int, const=CHECK_INTERVAL, default=None,
                    metavar="SECONDS", help="keep running, check every SECONDS (default %(const)s)")
    ap.add_argument("--dry-run", action="store_true", help="show what matches now; send and save nothing")
    ap.add_argument("--selftest", action="store_true", help="check Telegram, mymarket, photos, details, rate, AI")
    ap.add_argument("--resolve", metavar="URL", help="turn a mymarket.ge URL into a payload")
    ap.add_argument("--reset", action="store_true", help="forget everything seen so far")
    args = ap.parse_args()

    tool = args.dry_run or args.selftest or args.resolve
    setup_logging(to_file=not tool)

    if args.resolve:
        return cmd_resolve(args.resolve)
    if args.dry_run:
        return cmd_dry_run()
    if not BOT_TOKEN or not CHAT_ID:
        sys.exit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing (.env file or environment variables)")
    if args.selftest:
        return cmd_selftest()

    if args.reset:
        for f in (STATE_FILE, OLD_SEEN_FILE):
            if f.exists():
                f.unlink()
        log.info("State cleared")

    state = load_state()

    if args.loop is None:
        sys.exit(0 if guarded_run(state) else 1)

    lock = acquire_lock()
    if lock is None:
        sys.exit("The bot is already running on this computer (another --loop window). Close it first.")
    interval = max(60, args.loop)
    log.info("Loop mode: checking every %ds. Ctrl+C to stop.", interval)
    try:
        while True:
            ok = guarded_run(state)
            # back off while failing so a block is not made worse (max 30 min)
            wait = interval if ok else min(interval * (2 ** min(state["fail_count"], 3)), 1800)
            time.sleep(wait)
    except KeyboardInterrupt:
        log.info("Stopped")


if __name__ == "__main__":
    main()

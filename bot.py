#!/usr/bin/env python3
"""Clipping campaign alert bot.

Pulls the current list of Content Rewards ("Whop clipping") campaigns from an
Apify scraper, keeps only the ones that match YOUR rules (config.json), skips
ones you were already told about, and sends the rest to Telegram and/or Discord.

Usage:
  python bot.py              normal run (fetch -> filter -> notify)
  python bot.py --dry-run    same, but prints instead of sending and saves nothing
  python bot.py --inspect    show what the scraper returned and what the bot could read
  python bot.py --file x.json  use a saved JSON list instead of calling Apify
  python bot.py --selftest   offline test of the filtering/dedupe logic

Secrets come from environment variables (never put them in config.json):
  APIFY_TOKEN, APIFY_ACTOR (e.g. user~actor-name), optional APIFY_INPUT (JSON),
  optional APIFY_MAX_CHARGE_USD,
  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID  and/or  DISCORD_WEBHOOK_URL
"""
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
SEEN_PATH = HERE / "seen.json"
USER_AGENT = "campaign-alert-bot/1.0"

DEFAULTS = {
    "min_rate_per_1k": 2.0,        # dollars per 1,000 views
    "min_budget_left": 3000,       # dollars still unspent in the campaign
    "no_application_only": True,   # skip campaigns you must apply to
    "platforms": [],               # e.g. ["tiktok", "instagram"]; empty = any
    "min_days_left": 2,            # skip pots about to run dry (if the data says)
    "max_alerts_per_run": 8,
    "keywords_include": [],        # campaign name/creator must contain one of these
    "keywords_exclude": [],        # skip if name/creator contains any of these
}

PLATFORM_ALIASES = {
    "twitter": "x", "ig": "instagram", "insta": "instagram", "yt": "youtube",
    "youtubeshorts": "youtube", "shorts": "youtube", "tt": "tiktok",
}

# Field-name guesses, best first. Matching ignores case, spaces, _ and -, and
# also looks inside nested objects, so different scrapers usually just work.
ALIASES = {
    "id": ["id", "campaignId", "uuid"],
    "name": ["name", "title", "campaignName"],
    "url": ["url", "campaignUrl", "link"],
    "creator": ["creator", "creatorName", "brand", "owner"],
    "status": ["status"],
    "rate": ["rewardPer1kViewsUsd", "rewardPer1kViews", "rewardPer1k", "ratePer1kViews",
             "payoutPer1kViews", "cpmUsd", "cpm", "payoutRate"],
    "left": ["budgetRemainingUsd", "budgetLeftUsd", "remainingBudgetUsd", "budgetRemaining",
             "budgetLeft", "remainingBudget", "potLeft"],
    "total": ["budgetUsd", "totalBudgetUsd", "totalBudget", "budget"],
    "spent": ["budgetSpentUsd", "spentUsd", "budgetSpent", "spent"],
    "cap": ["maxPayoutPerClipUsd", "maxPayoutUsd", "maxPayoutPerClip", "maxPayout", "maximumPayout"],
    "days": ["estDaysOfBudgetLeft", "daysOfBudgetLeft", "daysLeft"],
    "app": ["requiresApplication", "applicationRequired", "mustApply", "requiresApply"],
    "platforms": ["platforms", "platform"],
}

DEAD_STATUSES = {"paused", "ended", "completed", "closed", "finished", "inactive", "expired", "cancelled"}


# ----------------------------------------------------------------- parsing --

def norm(key):
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def find(obj, names):
    """First non-empty value whose key matches one of `names` (shallowest wins)."""
    flat = {}

    def walk(o, depth):
        if depth > 4 or not isinstance(o, dict):
            return
        for k, v in o.items():
            flat.setdefault(norm(k), v)
        for v in o.values():
            walk(v, depth + 1)

    walk(obj, 0)
    for name in names:
        v = flat.get(norm(name))
        if v is not None and v != "" and v != []:
            return v
    return None


def to_float(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        v = list(v.values())
    if isinstance(v, list):
        nums = [x for x in (to_float(i) for i in v) if x is not None]
        return max(nums) if nums else None
    m = re.search(r"-?\d[\d,]*\.?\d*", str(v))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def to_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "yes", "1", "required"):
            return True
        if s in ("false", "no", "0", "open", "none"):
            return False
    return None


def norm_platform(p):
    n = norm(p)
    return PLATFORM_ALIASES.get(n, n)


def to_platforms(v):
    if isinstance(v, dict):
        raw = list(v.keys())
    elif isinstance(v, list):
        raw = [(x.get("platform") or x.get("name") or "") if isinstance(x, dict) else x for x in v]
    elif isinstance(v, str):
        raw = re.split(r"[,/|]", v)
    else:
        raw = []
    return {p for p in (norm_platform(x) for x in raw) if p}


def text_of(v):
    if isinstance(v, dict):
        v = v.get("name") or v.get("title") or v.get("username") or ""
    return "" if v is None else str(v)


def parse(item):
    left = to_float(find(item, ALIASES["left"]))
    if left is None:
        total, spent = to_float(find(item, ALIASES["total"])), to_float(find(item, ALIASES["spent"]))
        if total is not None:
            left = max(total - (spent or 0.0), 0.0)
    url = text_of(find(item, ALIASES["url"]))
    status = text_of(find(item, ALIASES["status"])).lower()
    return {
        "id": text_of(find(item, ALIASES["id"])) or url,
        "name": text_of(find(item, ALIASES["name"])) or "(unnamed campaign)",
        "url": url,
        "creator": text_of(find(item, ALIASES["creator"])),
        "rate": to_float(find(item, ALIASES["rate"])),
        "left": left,
        "cap": to_float(find(item, ALIASES["cap"])),
        "days": to_float(find(item, ALIASES["days"])),
        "app": to_bool(find(item, ALIASES["app"])),
        "platforms": to_platforms(find(item, ALIASES["platforms"])),
        "dead": status in DEAD_STATUSES,
    }


# --------------------------------------------------------------- filtering --

def passes(c, cfg):
    if c["dead"]:
        return False
    if c["rate"] < cfg["min_rate_per_1k"] or c["left"] < cfg["min_budget_left"]:
        return False
    if cfg["no_application_only"] and c["app"] is True:
        return False
    want = {norm_platform(p) for p in cfg["platforms"]}
    if want and c["platforms"] and not (c["platforms"] & want):
        return False
    if c["days"] is not None and c["days"] < cfg["min_days_left"]:
        return False
    haystack = f"{c['name']} {c['creator']}".lower()
    inc = [k.lower() for k in cfg["keywords_include"]]
    if inc and not any(k in haystack for k in inc):
        return False
    if any(k.lower() in haystack for k in cfg["keywords_exclude"]):
        return False
    return True


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    return cfg


# ------------------------------------------------------------------- state --

def load_state(path):
    try:
        data = json.loads(Path(path).read_text())
        return {"ids": list(data.get("ids", [])), "last_health_alert": data.get("last_health_alert", "")}
    except (OSError, ValueError):
        return {"ids": [], "last_health_alert": ""}


def save_state(path, state):
    state["ids"] = state["ids"][-5000:]
    Path(path).write_text(json.dumps(state, indent=1))


# ------------------------------------------------------------ notifications --

def http_post_json(url, payload, timeout=30):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def channels_configured():
    tg = os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")
    dc = os.environ.get("DISCORD_WEBHOOK_URL")
    return bool(tg or dc)


def notify(text):
    """Send to every configured channel. Raises if every configured channel failed."""
    errors, sent = [], 0
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        try:
            http_post_json(f"https://api.telegram.org/bot{tok}/sendMessage",
                           {"chat_id": chat, "text": text, "disable_web_page_preview": True})
            sent += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"telegram: {e}")
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    if hook:
        try:
            http_post_json(hook, {"content": text[:1900]})
            sent += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"discord: {e}")
    if not sent:
        raise RuntimeError("; ".join(errors) or "no notification channel configured")


def format_alert(c):
    lines = [f"🔥 {c['name']}", f"${c['rate']:.2f} per 1K views · ${c['left']:,.0f} left"
             + (f" · ${c['cap']:,.0f} max per clip" if c["cap"] else "")]
    if c["platforms"]:
        lines.append("Platforms: " + ", ".join(sorted(c["platforms"])))
    lines.append({True: "Application: required", False: "Application: not needed"}.get(c["app"], "Application: unknown"))
    if c["days"] is not None:
        lines.append(f"Est. days of budget left: {c['days']:.0f}")
    if c["creator"]:
        lines.append(f"By: {c['creator']}")
    if c["url"]:
        lines.append(c["url"])
    return "\n".join(lines)


# ------------------------------------------------------------------ the run --

def run(items, cfg, state_path, send=True, persist=True):
    parsed = [parse(i) for i in items if isinstance(i, dict)]
    readable = [c for c in parsed if c["rate"] is not None and c["left"] is not None]
    print(f"Fetched {len(parsed)} campaigns; {len(readable)} had a readable rate and budget.")

    state = load_state(state_path)

    if parsed and not readable:
        msg = ("⚠️ Campaign bot fetched campaigns but could not read their rate/budget fields. "
               "The scraper's output format probably differs. Run: python bot.py --inspect")
        print(msg)
        today = date.today().isoformat()
        if send and channels_configured() and state["last_health_alert"] != today:
            try:
                notify(msg)
                state["last_health_alert"] = today
            except Exception as e:  # noqa: BLE001
                print(f"Could not send health alert: {e}")
        if persist:
            save_state(state_path, state)
        return []

    matches = [c for c in readable if passes(c, cfg)]
    matches.sort(key=lambda c: (-c["rate"], -c["left"]))
    seen = set(state["ids"])
    fresh = [c for c in matches if c["id"] not in seen]
    batch = fresh[: cfg["max_alerts_per_run"]]
    print(f"{len(matches)} match your rules; {len(fresh)} are new; sending {len(batch)}.")

    if send and batch and not channels_configured():
        sys.exit("No notification channel set. Add TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID or DISCORD_WEBHOOK_URL.")

    delivered, failures = [], 0
    for c in batch:
        text = format_alert(c)
        if not send:
            print("---\n" + text)
            delivered.append(c)
            continue
        try:
            notify(text)
            delivered.append(c)
            time.sleep(1)
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"Failed to send '{c['name']}': {e}")

    state["ids"].extend(c["id"] for c in delivered)
    if persist:
        save_state(state_path, state)
    if batch and failures == len(batch) and send:
        sys.exit(1)
    return delivered


# ------------------------------------------------------------------- fetch --

def fetch_apify():
    token = os.environ.get("APIFY_TOKEN")
    actor = os.environ.get("APIFY_ACTOR", "").replace("/", "~")
    if not token or not actor:
        sys.exit("Set APIFY_TOKEN and APIFY_ACTOR (like username~actor-name).")
    payload = json.loads(os.environ.get("APIFY_INPUT") or '{"maxItems": 100}')
    query = {"token": token}
    if os.environ.get("APIFY_MAX_CHARGE_USD"):
        query["maxTotalChargeUsd"] = os.environ["APIFY_MAX_CHARGE_USD"]
    url = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?" + urllib.parse.urlencode(query)
    try:
        return json.loads(http_post_json(url, payload, timeout=330))
    except urllib.error.HTTPError as e:
        sys.exit(f"Apify returned HTTP {e.code}: {e.read()[:300].decode(errors='replace')}")


def inspect(items):
    print(f"Items returned: {len(items)}")
    if not items:
        return
    print("\nFirst item as returned by the scraper:")
    print(json.dumps(items[0], indent=1)[:1800])
    print("\nWhat the bot understood from the first 3 items:")
    for i in items[:3]:
        c = parse(i)
        print({k: (sorted(v) if isinstance(v, set) else v) for k, v in c.items()})
    ok = sum(1 for i in items if (lambda c: c["rate"] is not None and c["left"] is not None)(parse(i)))
    print(f"\nReadable (rate + budget found): {ok} of {len(items)}")
    print("If a field shows None but exists above, add its name to ALIASES in bot.py.")


# ---------------------------------------------------------------- self-test --

SAMPLE = [
    {"id": "a1", "name": "Good A", "url": "https://example.com/a1", "status": "active",
     "rewardPer1kViewsUsd": 3.0, "budgetRemainingUsd": 12000, "maxPayoutPerClipUsd": 500,
     "requiresApplication": False, "platforms": ["tiktok", "instagram"], "estDaysOfBudgetLeft": 20},
    {"campaignId": "n1", "title": "Nested B", "campaignUrl": "https://example.com/n1",
     "economics": {"rewardPer1kViewsUsd": "$2.50", "budgetUsd": 20000, "budgetSpentUsd": 5000},
     "platforms": ["TikTok", "Twitter"]},
    {"id": "lo", "name": "Low rate", "rewardPer1kViewsUsd": 1.0, "budgetRemainingUsd": 50000},
    {"id": "poor", "name": "Low budget", "rewardPer1kViewsUsd": 3.0, "budgetRemainingUsd": 500},
    {"id": "app", "name": "Needs app", "rewardPer1kViewsUsd": 4.0, "budgetRemainingUsd": 9000, "requiresApplication": True},
    {"id": "dry", "name": "Dying", "rewardPer1kViewsUsd": 3.5, "budgetRemainingUsd": 5000, "estDaysOfBudgetLeft": 0.5},
    {"id": "pool", "name": "Pool", "payoutType": "pool", "rewardPer1kViewsUsd": None, "budgetUsd": 5000},
    {"id": "off", "name": "Paused", "status": "paused", "rewardPer1kViewsUsd": 3, "budgetRemainingUsd": 9000},
]


def selftest():
    cfg = dict(DEFAULTS)
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "seen.json"
        first = run(SAMPLE, cfg, path, send=False, persist=True)
        names = [c["name"] for c in first]
        assert names == ["Good A", "Nested B"], names
        second = run(SAMPLE, cfg, path, send=False, persist=True)
        assert second == [], "duplicates were re-sent"
        tt_only = dict(cfg, platforms=["x"])
        assert [c["name"] for c in run(SAMPLE, tt_only, Path(d) / "s2.json", send=False)] == ["Nested B"]
        broken = run([{"foo": 1}, {"bar": 2}], cfg, Path(d) / "s3.json", send=False, persist=False)
        assert broken == []
    print("\nSELFTEST PASSED")


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        return selftest()
    cfg = load_config()
    if "--file" in args:
        items = json.loads(Path(args[args.index("--file") + 1]).read_text())
    else:
        items = fetch_apify()
    if isinstance(items, dict):
        items = items.get("items") or items.get("data") or [items]
    if "--inspect" in args:
        return inspect(items)
    dry = "--dry-run" in args
    run(items, cfg, SEEN_PATH, send=not dry, persist=not dry)


if __name__ == "__main__":
    main()

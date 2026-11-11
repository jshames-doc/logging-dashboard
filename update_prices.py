"""Refresh pricing_data.json from the published Devin Desktop models page.

    python update_prices.py              # fetch, show changes, write
    python update_prices.py --dry-run    # fetch and show changes only
    python update_prices.py --file page.html   # parse a saved copy of the page

The page embeds a `modelCostData` array of
{tier, model_uid, label, *_cost_per_million_usd, ...} objects. We keep the
Devin Enterprise tier, and fall back to the Pro tier for models only published
there. Models that vanish from the page are kept at their last-known rates with
tier "Legacy", so historical sessions stay priced. The dashboard reloads
pricing_data.json automatically; just refresh the page.
"""
import argparse
import json
import re
import sys
import urllib.request
from datetime import date
from pathlib import Path

URL = "https://docs.devin.ai/desktop/models"
# Primary tier candidates in preference order (the site renamed
# TEAMS_TIER_ENTERPRISE_SAAS to TEAMS_TIER_DEVIN_ENTERPRISE in late 2025).
TIERS = {"TEAMS_TIER_DEVIN_ENTERPRISE": "Devin Enterprise",
         "TEAMS_TIER_ENTERPRISE_SAAS": "Enterprise SaaS"}
FALLBACK_TIER, FALLBACK_NAME = "TEAMS_TIER_PRO", "Pro"
OUT = Path(__file__).resolve().parent / "pricing_data.json"
COSTS = ("input", "output", "cache_write", "cache_read")

_OBJ = re.compile(r'\{"tier":[^{}]*\}')
_FIELD = re.compile(r'"(\w+)":(`[^`]*`|"(?:[^"\\]|\\.)*"|[-+0-9.eE]+|true|false|null)')


def _value(raw):
    if raw[0] in "`\"":
        return raw[1:-1]
    if raw in ("true", "false", "null"):
        return {"true": True, "false": False, "null": None}[raw]
    return float(raw)


def parse(html):
    """Return de-duplicated cost entries from the page HTML."""
    text = html.replace('\\"', '"')
    seen, entries = set(), []
    for obj in _OBJ.findall(text):
        f = {k: _value(v) for k, v in _FIELD.findall(obj)}
        try:
            entry = {"tier": f["tier"], "uid": f["model_uid"], "label": f["label"],
                     **{c: float(f[f"{c}_cost_per_million_usd"] or 0) for c in COSTS}}
        except KeyError:
            continue
        key = (entry["tier"], entry["uid"])
        if key not in seen:
            seen.add(key)
            entries.append(entry)
    return entries


def build(entries, fetched=None):
    """Primary-tier entries, plus fallback-tier entries for labels/uids missing from the primary tier."""
    tier = next((t for t in TIERS if any(e["tier"] == t for e in entries)), None)
    primary = [e for e in entries if e["tier"] == tier]
    labels, uids = {e["label"] for e in primary}, {e["uid"] for e in primary}
    extra = [e for e in entries if e["tier"] == FALLBACK_TIER and e["label"] not in labels and e["uid"] not in uids]
    names = {**TIERS, FALLBACK_TIER: FALLBACK_NAME}
    models = [{**{k: e[k] for k in ("uid", "label", *COSTS)}, "tier": names[e["tier"]]}
              for e in sorted(primary + extra, key=lambda e: (e["label"].lower(), e["uid"]))]
    return {"source": URL, "tier": TIERS.get(tier, "?"), "fallback_tier": FALLBACK_NAME,
            "fetched": fetched or date.today().isoformat(), "models": models}


def merge_legacy(old, new):
    """Append models from the previous snapshot that the page no longer publishes,
    marked tier "Legacy" at their last-known rates. Returns the kept entries."""
    labels = {m["label"] for m in new["models"]}
    uids = {m["uid"] for m in new["models"]}
    kept = [{**m, "tier": "Legacy"} for m in (old or {}).get("models", [])
            if m["label"] not in labels and m.get("uid") not in uids]
    new["models"] = sorted(new["models"] + kept,
                           key=lambda m: (m["label"].lower(), m.get("uid") or ""))
    return kept


def diff(old, new):
    """Human-readable changes between two pricing_data dicts, keyed by label."""
    rates = lambda d: {m["label"]: tuple(m[c] for c in COSTS) for m in (d or {}).get("models", [])}
    o, n = rates(old), rates(new)
    fmt = lambda r: "/".join(f"{v:g}" for v in r)
    lines = [f"+ {k}: {fmt(n[k])}" for k in sorted(n.keys() - o.keys())]
    lines += [f"- {k}: {fmt(o[k])}" for k in sorted(o.keys() - n.keys())]
    lines += [f"~ {k}: {fmt(o[k])} -> {fmt(n[k])}" for k in sorted(o.keys() & n.keys()) if o[k] != n[k]]
    return lines


def fetch(url=URL):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (session-ledger price updater)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", errors="replace")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", help="parse a saved HTML file instead of fetching")
    ap.add_argument("--dry-run", action="store_true", help="show changes without writing")
    args = ap.parse_args(argv)

    html = Path(args.file).read_text(encoding="utf-8", errors="replace") if args.file else fetch()
    entries = parse(html)
    if not any(e["tier"] in TIERS for e in entries):
        sys.exit("No primary-tier prices found - the page layout may have changed. pricing_data.json left untouched.")
    new = build(entries)
    old = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else None

    changes = diff(old, new)
    print(f"{len(new['models'])} models published (USD per 1M tokens: input/output/cache write/cache read)")
    print("\n".join(changes) if changes else "No price changes.")
    kept = merge_legacy(old, new)
    if kept:
        print(f"{len(kept)} models no longer published - retained at last rates (tier \"Legacy\")")
    if args.dry_run:
        return
    OUT.write_text(json.dumps(new, indent=1) + "\n", encoding="utf-8")
    print(f"Wrote {OUT.name} (snapshot {new['fetched']}). Refresh the dashboard to apply.")


if __name__ == "__main__":
    main()

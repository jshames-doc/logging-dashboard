"""Refresh pricing_data.json from the Devin model catalog RPC plus the docs page.

    python update_prices.py                 # fetch catalog + docs, show changes, write
    python update_prices.py --dry-run       # fetch and show changes only
    python update_prices.py --docs-only     # recovery path: docs page only, no RPC
    python update_prices.py --file page.html          # saved docs HTML
    python update_prices.py --catalog-file resp.json  # saved GetCliModelConfigs response

Primary source: POST {api_server_url}/exa.api_server_pb.ApiServerService/GetCliModelConfigs
with the Devin CLI's credentials.toml. The server wants the windsurf IDE
identity *with a plausible version* - a placeholder like 0.0.0 returns an
empty catalog, which is treated as a failure (no write), never as zero models.
COST-kind model_dimensions labelled Input / Cached input / Output with
denominator "1M tokens" carry USD per-million-token rates (verified: every
overlapping UID matches the published docs rates exactly).

The catalog has no cache-write rate, so cache_write comes from (1) the docs
page's selected tier by exact UID, then (2) a trusted previous-snapshot value
for that UID. Models with no trusted cache-write rate stay unpriced rather
than getting a guessed rate.

The docs page (docs.devin.ai/desktop/models) still covers models missing from
the catalog, and --docs-only restores the all-docs flow for use when the RPC
is down or the account returns an empty catalog. pricing_manual.json entries
active under the previous snapshot keep their models: generated candidates
colliding with an active manual label or any of its uids are deferred with a
notice. `--promote-manual` performs the one-time migration instead: colliding
models are emitted from the catalog/docs, their cache_write may fall back to
the manual entry's own rate (kind "manual"), long_prompt carries over, and a
tier "Manual" entry keeps each promoted entry's uncovered aliases priced.
Models that vanish everywhere are kept as tier "Legacy" only when
their rates and provenance are trusted. Per-rate provenance is recorded in
each model's rate_sources; a reused value keeps its original source and date.
"""
import argparse
import json
import math
import os
import re
import sys
import urllib.request
from datetime import date
from pathlib import Path

import server

URL = "https://docs.devin.ai/desktop/models"
# Primary docs tier = the page's "Self-serve" tab. The "Enterprise (ACUs)" tab
# is the fallback for models only published there (TEAMS_TIER_ENTERPRISE_SAAS
# was its name before the late-2025 rename).
TIERS = {"TEAMS_TIER_PRO": "Self-serve"}
FALLBACK_TIERS = {"TEAMS_TIER_DEVIN_ENTERPRISE": "Devin Enterprise",
                  "TEAMS_TIER_ENTERPRISE_SAAS": "Enterprise SaaS"}
TIER_NAMES = {**TIERS, **FALLBACK_TIERS}
OUT = Path(__file__).resolve().parent / "pricing_data.json"
MANUAL = Path(__file__).resolve().parent / "pricing_manual.json"
COSTS = ("input", "output", "cache_write", "cache_read")

CATALOG_RPC = "/exa.api_server_pb.ApiServerService/GetCliModelConfigs"
CATALOG_SRC = "GetCliModelConfigs"
# The server rejects placeholder versions with an empty catalog, so send the
# installed Devin client's own version (product.json codeiumVersion);
# DEVIN_IDE_VERSION overrides, and CATALOG_IDE_FALLBACK is the last resort.
CATALOG_IDE_FALLBACK = "1.48.2"
PRODUCT_JSON = (Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Devin" / "resources"
                / "app" / "product.json")
COST_KIND = "MODEL_DIMENSION_KIND_COST"
# Only units verified to be USD/token may be consumed; anything else rejects.
USD_DENOM = "1M tokens"
USD_PRICING = {"MODEL_PRICING_TYPE_STATIC_CREDIT"}
DIM_TO_COST = {"Input": "input", "Output": "output", "Cached input": "cache_read"}
CATALOG_COSTS = tuple(DIM_TO_COST.values())

TRUSTED_KINDS = {"catalog", "docs", "manual"}
# Pre-migration snapshots (no rate_sources) are trusted only for these tiers;
# "Live·est" or any unknown tier is not a valid fallback and cannot be Legacy.
TRUSTED_TIERS = {*TIERS.values(), *FALLBACK_TIERS.values(), "Legacy"}

_OBJ = re.compile(r'\{"tier":[^{}]*\}')
_FIELD = re.compile(r'"(\w+)":(`[^`]*`|"(?:[^"\\]|\\.)*"|[-+0-9.eE]+|true|false|null)')


def _num(v):
    """v as a finite, nonnegative float, or None. Explicit zero is valid."""
    return (float(v) if isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v) and v >= 0 else None)


def _value(raw):
    if raw[0] in "`\"":
        return raw[1:-1]
    if raw in ("true", "false", "null"):
        return {"true": True, "false": False, "null": None}[raw]
    return float(raw)


def parse(html):
    """Return de-duplicated cost entries from the docs page HTML."""
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


def fetch_docs(url=URL):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (session-ledger price updater)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", errors="replace")


def select_docs(entries):
    """Chosen primary-tier entries plus fallback-tier entries for uids/labels
    missing from it, keyed by uid for exact-UID lookups. Returns
    (primary tier name or None, {uid: entry-with-display-tier})."""
    tier = next((t for t in TIERS if any(e["tier"] == t for e in entries)), None)
    primary = [e for e in entries if e["tier"] == tier]
    labels, uids = {e["label"] for e in primary}, {e["uid"] for e in primary}
    by_uid = {e["uid"]: {**e, "tier": TIER_NAMES[e["tier"]]} for e in primary}
    for e in entries:
        if e["tier"] in FALLBACK_TIERS and e["label"] not in labels and e["uid"] not in uids:
            by_uid.setdefault(e["uid"], {**e, "tier": TIER_NAMES[e["tier"]]})
    return tier, by_uid


def ide_version(product=PRODUCT_JSON):
    """Version string the catalog RPC will accept (see CATALOG_IDE_FALLBACK)."""
    if os.environ.get("DEVIN_IDE_VERSION"):
        return os.environ["DEVIN_IDE_VERSION"]
    try:
        v = json.loads(Path(product).read_text(encoding="utf-8")).get("codeiumVersion")
    except (OSError, ValueError, AttributeError):
        v = None
    return v if isinstance(v, str) and re.fullmatch(r"\d+\.\d+\.\d+", v) else CATALOG_IDE_FALLBACK


def fetch_catalog(creds):
    """GetCliModelConfigs with the CLI's credentials.toml. Never logs the key."""
    key, base = creds
    ver = ide_version()
    body = json.dumps({"metadata": {"api_key": key, "ide_name": "windsurf", "ide_version": ver,
                                    "extension_name": "windsurf", "extension_version": ver}}).encode()
    req = urllib.request.Request(base + CATALOG_RPC, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def catalog_models(resp):
    """Validated USD catalog rates: {uid: {uid,label,input,output,cache_read}}.

    Selects COST-kind dimensions by recognized label with the verified
    USD/1M-token unit. Raises ValueError on a malformed response; malformed or
    ambiguous entries are rejected and counted by reason."""
    if not isinstance(resp, dict):
        raise ValueError("response is not a JSON object")
    raw = resp.get("clientModelConfigs")
    if raw is not None and not isinstance(raw, list):
        raise ValueError("clientModelConfigs is not a list")
    models, rejected, poisoned = {}, {}, set()

    def reject(reason):
        rejected[reason] = rejected.get(reason, 0) + 1

    for cfg in raw or []:
        if not isinstance(cfg, dict):
            reject("config is not an object")
            continue
        uid, label = cfg.get("modelUid"), cfg.get("label")
        if not (isinstance(uid, str) and uid.strip() and isinstance(label, str) and label.strip()):
            reject("missing uid or label")
            continue
        if cfg.get("pricingType") not in USD_PRICING:
            reject("unsupported pricingType " + repr(cfg.get("pricingType")))
            continue
        rates, seen, bad = {}, set(), None
        for dim in cfg.get("modelDimensions") or []:
            if not isinstance(dim, dict) or dim.get("kind") != COST_KIND or dim.get("label") not in DIM_TO_COST:
                continue
            key = DIM_TO_COST[dim["label"]]
            if key in seen:
                bad = "duplicate " + dim["label"] + " dimension"
                break
            seen.add(key)
            if dim.get("denominator") != USD_DENOM:
                bad = "unsupported denominator " + repr(dim.get("denominator"))
                break
            v = _num(dim.get("value"))
            if v is None:
                bad = "invalid " + dim["label"] + " value"
                break
            rates[key] = v
        if bad:
            reject(bad)
            continue
        missing = [c for c in CATALOG_COSTS if c not in rates]
        if not rates:
            reject("no price dimensions")
            continue
        if missing:
            reject("missing " + "/".join(missing) + " dimension")
            continue
        entry = {"uid": uid, "label": label, **rates}
        if uid in models or uid in poisoned:
            if uid in poisoned or models[uid] != entry:
                models.pop(uid, None)
                poisoned.add(uid)
                reject("conflicting duplicate uid " + uid)
            continue  # identical duplicates dedupe
        models[uid] = entry
    return models, rejected


def _uids_of(m):
    """Lowercased slugs for an entry: 'uids' list (manual) or single 'uid'."""
    u = m.get("uids") or ([m["uid"]] if isinstance(m.get("uid"), str) else [])
    return [x.lower() for x in u if isinstance(x, str)] if isinstance(u, list) else []


def _valid_rates(m):
    """All four rate fields of a model as finite nonneg floats, or None."""
    vals = [_num(m.get(c)) for c in COSTS]
    return dict(zip(COSTS, vals)) if all(v is not None for v in vals) else None


def _active_manual(manual, old):
    """pricing_manual.json entries that are ACTIVE under the previous generated
    snapshot (the loader's label/UID overlap rule drops an entry on any
    collision, so active = zero overlap). Shadowed entries stay shadowed."""
    old_models = (old or {}).get("models") or []
    labels = {m.get("label") for m in old_models if isinstance(m, dict)}
    uids = {u for m in old_models if isinstance(m, dict) for u in _uids_of(m)}
    return [m for m in (manual or {}).get("models") or []
            if isinstance(m, dict) and isinstance(m.get("label"), str)
            and m["label"] not in labels and not set(_uids_of(m)) & uids]


def _manual_src(m):
    """Provenance for a rate taken from pricing_manual.json."""
    s = {"kind": "manual"}
    if isinstance(m.get("noted"), str):
        s["date"] = m["noted"]
    return s


def _trusted_key(m, key, old_fetched):
    """The rate_sources entry for one rate if its provenance is trusted.

    Pre-migration entries (no rate_sources) are trusted only for the known
    docs tiers, with provenance inferred as docs at the old snapshot's date."""
    rs = m.get("rate_sources")
    if rs is None:
        return ({"kind": "docs", "tier": m["tier"], "date": old_fetched}
                if m.get("tier") in TRUSTED_TIERS else None)
    s = rs.get(key) if isinstance(rs, dict) else None
    return s if isinstance(s, dict) and s.get("kind") in TRUSTED_KINDS else None


def _trusted_sources(m, old_fetched):
    """Complete trusted rate_sources for a prior model, or None."""
    out = {c: _trusted_key(m, c, old_fetched) for c in COSTS}
    return out if all(v is not None for v in out.values()) else None


def _legacy(old, models, plabels, puids, stats):
    """Retained previous-snapshot models with trusted rates+provenance, as
    tier "Legacy", preserving their rate_sources. Appends to models."""
    labels = {m["label"] for m in models}
    uids = {u for m in models for u in _uids_of(m)}
    old_fetched = (old or {}).get("fetched")
    for om in sorted((old or {}).get("models", []),
                     key=lambda m: (str(m.get("label")).lower(), str(m.get("uid") or ""))
                     if isinstance(m, dict) else ("", "")):
        if not isinstance(om, dict) or not isinstance(om.get("label"), str):
            continue
        if om["label"] in labels or set(_uids_of(om)) & uids:
            continue
        if om["label"] in plabels or set(_uids_of(om)) & puids:
            stats["protected"].append(om.get("uid") or om["label"])
            continue
        rs, rates = _trusted_sources(om, old_fetched), _valid_rates(om)
        if rs is None or rates is None:
            stats["dropped"] += 1
            continue
        tier = "Manual" if all(s.get("kind") == "manual" for s in rs.values()) else "Legacy"
        m = {**{k: om[k] for k in ("uid", "uids", "label", "long_prompt", "source", "noted")
                if k in om}, **rates, "tier": tier, "rate_sources": rs}
        models.append(m)
        labels.add(m["label"])
        uids.update(_uids_of(m))
        stats["legacy" if tier == "Legacy" else "manual_retained"] += 1


def merge(catalog, docs_by_uid, old, manual, fetched, promote=False):
    """New model list: catalog models (docs/reused cache_write), docs coverage
    for out-of-catalog uids, and trusted Legacy retention. An empty catalog is
    the docs-only path: every validated docs entry is emitted.

    With promote=True, manual protection is suspended (--promote-manual):
    colliding catalog/docs candidates are emitted, an active manual entry's
    cache_write fills catalog gaps as a third fallback (kind "manual"), its
    long_prompt is carried over, and a tier "Manual" entry keeps its
    catalog-uncovered uids priced."""
    active = _active_manual(manual, old)
    active_labels = {m["label"] for m in active}
    active_uids = {u for m in active for u in _uids_of(m)}
    plabels, puids = (set(), set()) if promote else (active_labels, active_uids)
    man_uid = {u: m for m in active for u in _uids_of(m)} if promote else {}
    man_label = {m["label"]: m for m in active} if promote else {}
    old_fetched = (old or {}).get("fetched")
    old_by_uid = {m["uid"]: m for m in (old or {}).get("models", [])
                  if isinstance(m, dict) and isinstance(m.get("uid"), str)}
    stats = {"protected": [], "missing_cw": [], "legacy": 0, "dropped": 0, "docs_rejected": 0,
             "promoted": [], "manual_kept": 0, "manual_retained": 0}
    models = []
    for uid in sorted(catalog):
        cm = catalog[uid]
        collides = cm["label"] in active_labels or uid.lower() in active_uids
        if collides and not promote:
            stats["protected"].append(uid)
            continue
        if collides:
            stats["promoted"].append(uid)
        src = {"kind": "catalog", "src": CATALOG_SRC, "date": fetched}
        rs = {c: dict(src) for c in CATALOG_COSTS}
        cw, cwsrc, tier = None, None, "Live·cached"
        d = docs_by_uid.get(uid)
        if d is not None and _num(d.get("cache_write")) is not None:
            cw, cwsrc, tier = _num(d["cache_write"]), {"kind": "docs", "tier": d["tier"], "date": fetched}, "Live"
        else:
            om = old_by_uid.get(uid)
            if om is not None and _num(om.get("cache_write")) is not None:
                s = _trusted_key(om, "cache_write", old_fetched)
                if s is not None:
                    cw, cwsrc = _num(om["cache_write"]), s
            if cw is None:
                mm = man_uid.get(uid.lower()) or man_label.get(cm["label"])
                if mm is not None and _num(mm.get("cache_write")) is not None:
                    cw, cwsrc = _num(mm["cache_write"]), _manual_src(mm)
        if cw is None:
            stats["missing_cw"].append(uid)
            continue
        m = {"uid": uid, "label": cm["label"], "input": cm["input"],
             "output": cm["output"], "cache_write": cw, "cache_read": cm["cache_read"],
             "tier": tier, "rate_sources": {**rs, "cache_write": cwsrc}}
        # Published base rates do not replace a previously promoted long-prompt tier.
        lp = (man_uid.get(uid.lower()) or man_label.get(cm["label"])
              or old_by_uid.get(uid) or {}).get("long_prompt")
        if isinstance(lp, dict) and (long_rates := _valid_rates(lp)) is not None:
            m["long_prompt"] = long_rates
        models.append(m)
    for uid in sorted(docs_by_uid):
        if uid in catalog:
            continue  # already handled above (emitted or reported)
        d = docs_by_uid[uid]
        collides = d["label"] in active_labels or uid.lower() in active_uids
        if collides and not promote:
            stats["protected"].append(uid)
            continue
        if collides:
            stats["promoted"].append(uid)
        rates = _valid_rates(d)
        if rates is None:
            stats["docs_rejected"] += 1
            continue
        src = {"kind": "docs", "tier": d["tier"], "date": fetched}
        m = {"uid": uid, "label": d["label"], **rates, "tier": d["tier"],
             "rate_sources": {c: dict(src) for c in COSTS}}
        lp = (man_uid.get(uid.lower()) or man_label.get(d["label"])
              or old_by_uid.get(uid) or {}).get("long_prompt")
        if isinstance(lp, dict) and (long_rates := _valid_rates(lp)) is not None:
            m["long_prompt"] = long_rates
        models.append(m)
    if promote:
        covered_u = {u for m in models for u in _uids_of(m)}
        for mm in active:
            all_u = [u for u in mm.get("uids") or [] if isinstance(u, str)]
            leftover = [u for u in all_u if u.lower() not in covered_u]
            if not leftover or len(leftover) == len(all_u):
                continue  # untouched entries stay file-driven; only bridged aliases are emitted
            rates = _valid_rates(mm)
            if rates is None:
                stats["dropped"] += 1
                continue
            src = _manual_src(mm)
            entry = {"label": mm["label"], "uids": leftover, **rates, "tier": "Manual",
                     "rate_sources": {c: dict(src) for c in COSTS}}
            for k in ("source", "noted", "long_prompt"):
                if k in mm:
                    entry[k] = mm[k]
            models.append(entry)
            covered_u.update(u.lower() for u in leftover)
            stats["manual_kept"] += 1
    _legacy(old, models, plabels, puids, stats)
    return models, stats


def validate(models):
    """Final sanity: no duplicate uids, no same-label different-rate pairs."""
    uids, by_label = set(), {}
    for m in models:
        uid = m.get("uid")
        if isinstance(uid, str):
            if uid in uids:
                raise ValueError("duplicate uid " + uid)
            uids.add(uid)
        prev = by_label.get(m["label"])
        if prev is not None and any(prev[c] != m[c] for c in COSTS):
            raise ValueError(f"label {m['label']!r} has conflicting rates")
        by_label.setdefault(m["label"], m)


def _src_summary(m):
    """Compact provenance signature, e.g. 'catalog+docs'."""
    rs = m.get("rate_sources")
    if not isinstance(rs, dict):
        return m.get("tier") or "?"
    kinds = {s.get("kind") for s in rs.values() if isinstance(s, dict)}
    return "+".join(sorted(kinds)) if kinds else (m.get("tier") or "?")


def diff(old, new):
    """Human-readable changes between two pricing_data dicts, keyed by uid."""
    def by_uid(d):
        return {m.get("uid") or m["label"]: m for m in (d or {}).get("models", [])
                if isinstance(m, dict) and isinstance(m.get("label"), str)}
    o, n = by_uid(old), by_uid(new)
    fmt = lambda m: "/".join(f"{m.get(c, 0):g}" for c in COSTS)
    lines = []
    for k in sorted(n.keys() - o.keys()):
        m = n[k]
        lines.append(f"+ {k} ({m['label']}): {fmt(m)} [{m.get('tier', '?')}]")
    for k in sorted(o.keys() - n.keys()):
        m = o[k]
        lines.append(f"- {k} ({m['label']}): {fmt(m)} [{m.get('tier', '?')}]")
    for k in sorted(o.keys() & n.keys()):
        a, b = o[k], n[k]
        parts = []
        if any(a.get(c) != b.get(c) for c in COSTS):
            parts.append(f"{fmt(a)} -> {fmt(b)}")
        if a.get("tier") != b.get("tier"):
            parts.append(f"tier {a.get('tier')} -> {b.get('tier')}")
        if _src_summary(a) != _src_summary(b):
            parts.append(f"sources {_src_summary(a)} -> {_src_summary(b)}")
        if parts:
            lines.append(f"~ {k} ({b['label']}): " + ", ".join(parts))
    return lines


def write_snapshot(new, path=OUT):
    """Atomically replace the snapshot; a failed replace leaves the old file."""
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(new, indent=1) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _fail(msg):
    sys.exit(msg + " pricing_data.json left untouched.")


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # tier names like "Live·cached" must not crash a cp1252 pipe
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", help="parse a saved docs HTML file instead of fetching it")
    ap.add_argument("--catalog-file", help="parse a saved GetCliModelConfigs response instead of the RPC")
    ap.add_argument("--docs-only", action="store_true",
                    help="skip the catalog RPC entirely; docs prices plus trusted Legacy retention")
    ap.add_argument("--promote-manual", action="store_true",
                    help="one-time migration: let generated pricing override manual entries the "
                         "catalog now covers (uncovered manual aliases stay priced)")
    ap.add_argument("--dry-run", action="store_true", help="show changes without writing")
    args = ap.parse_args(argv)
    if args.docs_only and args.catalog_file:
        ap.error("--docs-only and --catalog-file are contradictory")
    fetched = date.today().isoformat()

    catalog, rejected = {}, {}
    if not args.docs_only:
        if args.catalog_file:
            try:
                resp = json.loads(Path(args.catalog_file).read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                _fail(f"Cannot read catalog fixture {args.catalog_file}: {e}.")
        else:
            creds = server._credentials(server.CREDENTIALS_PATH)
            if not creds:
                _fail("Devin credentials not found - sign in to the Devin CLI once, or use --docs-only.")
            try:
                resp = fetch_catalog(creds)
            except Exception as e:
                _fail(f"GetCliModelConfigs failed: {e} (use --docs-only to refresh from the docs page).")
        try:
            catalog, rejected = catalog_models(resp)
        except ValueError as e:
            _fail(f"Malformed catalog response: {e}.")
        if not resp.get("clientModelConfigs"):
            _fail("Catalog returned no model configs (account or version unsupported). "
                  "Use --docs-only to refresh from the docs page.")
        if not catalog:
            _fail("No usable USD catalog entries remain after validation.")

    if args.file:
        try:
            entries = parse(Path(args.file).read_text(encoding="utf-8", errors="replace"))
        except OSError as e:
            _fail(f"Cannot read docs fixture {args.file}: {e}.")
        if not entries:
            _fail(f"No cost entries parsed from {args.file} - not a saved models page?")
    else:
        try:
            entries = parse(fetch_docs())
        except Exception as e:
            if args.docs_only:
                _fail(f"Docs fetch failed: {e}.")
            print(f"warning: docs fetch failed ({e}); cache-write and coverage fall back to the "
                  "previous snapshot", file=sys.stderr)
            entries = []
    tier, docs = select_docs(entries)
    if args.docs_only and tier is None:
        _fail("No primary-tier prices found - the page layout may have changed.")

    old = None
    if OUT.exists():
        try:
            old = json.loads(OUT.read_text(encoding="utf-8"))
            if not isinstance(old, dict):
                raise ValueError("top level is not an object")
        except (OSError, ValueError) as e:
            _fail(f"Existing pricing_data.json is unreadable ({e}); refusing to discard it.")
    manual = None
    if MANUAL.exists():
        try:
            manual = json.loads(MANUAL.read_text(encoding="utf-8"))
            if not isinstance(manual, dict):
                raise ValueError("top level is not an object")
        except (OSError, ValueError) as e:
            _fail(f"pricing_manual.json is unreadable ({e}); refusing to risk displacing manual pricing.")

    models, stats = merge(catalog, docs, old, manual, fetched, promote=args.promote_manual)
    try:
        validate(models)
    except ValueError as e:
        _fail(f"Merged snapshot failed validation: {e}.")
    if not models:
        _fail("No complete usable models remain.")
    new = {"source": URL if args.docs_only else f"{CATALOG_SRC} + {URL}",
           "tier": TIER_NAMES.get(tier, "?") if args.docs_only else "Live",
           "fallback_tier": next(iter(FALLBACK_TIERS.values())),
           "fetched": fetched,
           "models": sorted(models, key=lambda m: (m["label"].lower(), m.get("uid") or ""))}

    where = "docs-only" if args.docs_only else f"{len(catalog)} catalog entries"
    print(f"{len(models)} models priced ({where}; USD per 1M tokens: input/output/cache write/cached input)")
    no_price = rejected.pop("no price dimensions", 0)
    if no_price:
        print(f"  catalog lists {no_price} entries without prices (docs/Legacy rates used where available)")
    for reason, n in sorted(rejected.items()):
        print(f"  catalog rejected: {n} x {reason}")
    if stats["protected"]:
        print(f"{len(stats['protected'])} models deferred to active pricing_manual.json entries: "
              + ", ".join(stats["protected"][:8]) + (" ..." if len(stats["protected"]) > 8 else ""))
    if stats["promoted"]:
        print(f"{len(stats['promoted'])} models promoted over manual entries: "
              + ", ".join(stats["promoted"][:8]) + (" ..." if len(stats["promoted"]) > 8 else ""))
    if stats["manual_kept"]:
        print(f"{stats['manual_kept']} manual entries retained for catalog-uncovered aliases (tier \"Manual\")")
    if stats["missing_cw"]:
        print(f"{len(stats['missing_cw'])} catalog models unpriced (no trusted cache-write rate): "
              + ", ".join(stats["missing_cw"][:8]) + (" ..." if len(stats["missing_cw"]) > 8 else ""))
    if stats["docs_rejected"]:
        print(f"{stats['docs_rejected']} docs entries rejected (invalid rates)")
    if stats["dropped"]:
        print(f"{stats['dropped']} prior models dropped (untrusted rates or provenance)")
    changes = diff(old, new)
    print("\n".join(changes) if changes else "No price changes.")
    if stats["legacy"]:
        print(f"{stats['legacy']} models no longer published - retained at last rates (tier \"Legacy\")")
    if stats["manual_retained"]:
        print(f"{stats['manual_retained']} manual-sourced aliases retained from the previous snapshot (tier \"Manual\")")
    if args.dry_run:
        return
    write_snapshot(new, OUT)
    print(f"Wrote {OUT.name} (snapshot {new['fetched']}). Refresh the dashboard to apply.")


if __name__ == "__main__":
    main()

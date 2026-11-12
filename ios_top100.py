#!/usr/bin/env python3
"""
Top 100 free iPhone apps (US App Store) for every app category AND every game
subcategory, written to Google Sheets (Colab) with a local CSV backup.

Pipeline
  1. Pull Apple's RSS top-chart for each genre (retries + on-disk cache).
  2. Enrich each unique app via Apple's Lookup API (retries).
  3. Write a CSV (atomic) and then a Google Sheet.

Hardening vs. the original
  - Retries with exponential backoff + jitter, honors Retry-After on 429/503
  - Per-chart cache so a rerun the same day skips what already succeeded
  - Failed charts, short charts and missing Lookup results are summarized at the end
  - snapshot_date column; `advisories` column renamed (it is not an IAP flag)
  - Atomic CSV write; Sheets write uses RAW so "=..." names can't run as formulas
  - Small thread pool for fetches, logging instead of print

DEDUPE_MODE:
  "merge":    one row per app. type / category comma-joined (best-ranked first),
              best_rank, chart_count, chart_appearances.
  "separate": one row per app per chart, with a plain `rank` column.
"""

import csv
import json
import logging
import os
import random
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
CHART = "topfreeapplications"  # or "toppaidapplications", "topgrossingapplications"
LIMIT = 100
COUNTRY = "us"
ENRICH = True                  # False = skip Lookup API (faster, fewer fields)
INCLUDE_DESCRIPTIONS = False   # full descriptions make the output very large
DEDUPE_MODE = "merge"          # "merge" or "separate"

MAX_WORKERS = 3                # keep low to avoid rate limits
MAX_RETRIES = 5
REQUEST_TIMEOUT = 30
MIN_EXPECTED_FRACTION = 0.8    # warn if a chart returns < 80% of LIMIT

USE_CACHE = True
CACHE_DIR = os.environ.get("CACHE_DIR", "/tmp/ios_chart_cache")   # temp disk; wiped when runtime resets
OUTPUT_FILE = "ios_top100_all_categories.csv"

# Google Sheets
WRITE_TO_SHEET = True
SNAPSHOT_DATE = date.today().isoformat()

# Every run adds a NEW tab to the SAME spreadsheet.
# Spreadsheet lookup order: SPREADSHEET_URL (if set) -> existing file titled
# SPREADSHEET_TITLE in your Drive -> create it. So after the first run, future runs
# find the same file automatically; pasting the URL below just makes it explicit.
SPREADSHEET_URL = os.environ.get("SPREADSHEET_URL") or None   # required for unattended runs
SPREADSHEET_TITLE = "iOS Top 100 - All Categories"
WORKSHEET_NAME = f"top100_{SNAPSHOT_DATE}"   # one tab per day; a same-day rerun overwrites that tab

# Non-game categories
APP_GENRES = {
    "Books": 6018, "Business": 6000, "Developer Tools": 6026, "Education": 6017,
    "Entertainment": 6016, "Finance": 6015, "Food & Drink": 6023,
    "Graphics & Design": 6027, "Health & Fitness": 6013, "Lifestyle": 6012,
    "Magazines & Newspapers": 6021, "Medical": 6020, "Music": 6011,
    "Navigation": 6010, "News": 6009, "Photo & Video": 6008, "Productivity": 6007,
    "Reference": 6006, "Shopping": 6024, "Social Networking": 6005, "Sports": 6004,
    "Travel": 6003, "Utilities": 6002, "Weather": 6001,
}

# Game categories (type = "Games"). Top-level Games chart (6014) is not included.
GAME_GENRES = {
    "Action": 7001, "Adventure": 7002, "Casual": 7003, "Board": 7004,
    "Card": 7005, "Casino": 7006,
    "Family": 7009, "Music": 7011, "Puzzle": 7012,
    "Racing": 7013, "Role Playing": 7014, "Simulation": 7015, "Sports": 7016,
    "Strategy": 7017, "Trivia": 7018, "Word": 7019,
}

# One ordered job list: (type, category, genre_id)
JOBS = (
    [("Apps", name, gid) for name, gid in APP_GENRES.items()]
    + [("Games", name, gid) for name, gid in GAME_GENRES.items()]
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("ios_charts")


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------
def get_json(url, retries=MAX_RETRIES):
    """GET + parse JSON with exponential backoff, jitter, and Retry-After support."""
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code not in (403, 408, 429, 500, 502, 503, 504):
                raise  # e.g. 404: retrying won't help
            retry_after = e.headers.get("Retry-After") if e.headers else None
            delay = float(retry_after) if retry_after and retry_after.isdigit() \
                else 2 ** attempt + random.random()
        except Exception as e:  # timeouts, connection resets, bad JSON
            last_err = e
            delay = 2 ** attempt + random.random()

        if attempt < retries - 1:
            log.debug("retry %d/%d in %.1fs for %s (%s)", attempt + 1, retries, delay, url, last_err)
            time.sleep(delay)
    raise last_err


def dig(d, *keys, default=""):
    """Safely walk nested dicts."""
    for k in keys:
        if not isinstance(d, dict) or k not in d:
            return default
        d = d[k]
    return d


# --------------------------------------------------------------------------
# Step 1: charts
# --------------------------------------------------------------------------
def cache_path(genre_id):
    return os.path.join(CACHE_DIR, f"{COUNTRY}_{CHART}_{LIMIT}_{genre_id}_{SNAPSHOT_DATE}.json")


def fetch_chart(genre_id):
    """Return the list of raw RSS entries for a genre (cached per day)."""
    path = cache_path(genre_id)
    if USE_CACHE and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            log.warning("corrupt cache file %s, refetching", path)

    url = (f"https://itunes.apple.com/{COUNTRY}/rss/{CHART}/"
           f"limit={LIMIT}/genre={genre_id}/json")
    entries = dig(get_json(url), "feed", "entry", default=[])
    # RSS returns a dict instead of a list when there's only one entry
    entries = [entries] if isinstance(entries, dict) else entries

    if USE_CACHE and entries:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entries, f)
        os.replace(tmp, path)
    return entries


def run_job(job):
    app_type, category, gid = job
    return job, fetch_chart(gid)


def entry_to_row(e, app_type, category, rank):
    images = e.get("im:image", [])
    return {
        "type": app_type,
        "category": category,
        "rank": rank,
        "app_name": dig(e, "im:name", "label"),
        "developer": dig(e, "im:artist", "label"),
        "developer_url": dig(e, "im:artist", "attributes", "href"),
        "app_id": dig(e, "id", "attributes", "im:id"),
        "bundle_id": dig(e, "id", "attributes", "im:bundleId"),
        "app_url": dig(e, "id", "label"),
        "feed_release_date": dig(e, "im:releaseDate", "label"),
        "copyright": dig(e, "rights", "label"),
        "icon_url": images[-1].get("label", "") if images else "",
        "summary": dig(e, "summary", "label") if INCLUDE_DESCRIPTIONS else "",
    }


def collect_charts():
    """Fetch all charts concurrently. Returns (rows, failed_jobs, short_charts)."""
    results = {}
    failed, short = [], []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(run_job, job): job for job in JOBS}
        for fut in as_completed(futures):
            job = futures[fut]
            label = f"{job[0]} / {job[1]}"
            try:
                _, entries = fut.result()
            except Exception as e:
                log.error("FAILED %s: %s", label, e)
                failed.append((label, str(e)))
                continue

            if not entries:
                log.error("EMPTY chart %s (bad genre id or feed hiccup?)", label)
                failed.append((label, "empty chart"))
                continue

            if len(entries) < LIMIT * MIN_EXPECTED_FRACTION:
                log.warning("SHORT chart %s: %d of %d", label, len(entries), LIMIT)
                short.append((label, len(entries)))

            results[job] = entries
            log.info("%s: %d apps", label, len(entries))

    # Rebuild rows in the original JOBS order so output is deterministic
    rows = []
    for job in JOBS:
        for rank, e in enumerate(results.get(job, []), 1):
            rows.append(entry_to_row(e, job[0], job[1], rank))
    return rows, failed, short


# --------------------------------------------------------------------------
# Dedupe
# --------------------------------------------------------------------------
def merge_duplicates(rows):
    """Collapse rows that share an app_id into one row, joining their categories.

    Categories, types and chart_appearances are ordered by rank (best chart first).
    """
    out = []
    by_id = {}

    for r in rows:
        # Rows with no app_id can't be matched, so keep them as-is
        key = r["app_id"] or f"noid-{len(out)}"

        if key not in by_id:
            m = dict(r)
            m["_charts"] = []
            by_id[key] = m
            out.append(m)

        by_id[key]["_charts"].append((r["rank"], r["type"], r["category"]))

    for m in out:
        charts = sorted(m.pop("_charts"), key=lambda c: c[0])  # stable: ties keep chart order
        types, cats = [], []
        for _, t, c in charts:
            if t not in types:
                types.append(t)
            if c not in cats:
                cats.append(c)
        m["type"] = ", ".join(types)
        m["category"] = ", ".join(cats)
        m["best_rank"] = charts[0][0]
        m["chart_count"] = len(charts)
        m["chart_appearances"] = "; ".join(f"{t} > {c} #{rk}" for rk, t, c in charts)

    return out


# --------------------------------------------------------------------------
# Step 2: enrichment
# --------------------------------------------------------------------------
def lookup(app_ids):
    """Lookup API accepts many ids per call; batch to stay well under limits."""
    out, failed_batches = {}, 0
    ids = sorted(app_ids)
    for i in range(0, len(ids), 150):
        batch = ids[i:i + 150]
        url = (f"https://itunes.apple.com/lookup?id={','.join(batch)}"
               f"&country={COUNTRY}&entity=software")
        try:
            for r in get_json(url).get("results", []):
                out[str(r.get("trackId"))] = r
        except Exception as e:
            failed_batches += 1
            log.error("lookup batch %d failed: %s", i // 150 + 1, e)
        time.sleep(0.5)
    return out, failed_batches


EXTRA_COLS = [
    "avg_rating", "rating_count", "version", "current_version_release_date",
    "original_release_date", "min_ios_version", "age_rating",
    "apple_primary_genre", "all_genres", "seller_name", "seller_url",
    "languages_count", "advisories", "description",
]


def enrich(rows):
    """Add Lookup API fields to rows in place. Returns count of apps with no result."""
    app_ids = {r["app_id"] for r in rows if r["app_id"]}
    log.info("Enriching %d unique apps with Lookup API...", len(app_ids))
    info, failed_batches = lookup(app_ids)

    missing = 0
    for r in rows:
        d = info.get(r["app_id"])
        if d is None:
            missing += 1
            d = {}
        r.update({
            "avg_rating": d.get("averageUserRating", ""),
            "rating_count": d.get("userRatingCount", ""),
            "version": d.get("version", ""),
            "current_version_release_date": d.get("currentVersionReleaseDate", ""),
            "original_release_date": d.get("releaseDate", ""),
            "min_ios_version": d.get("minimumOsVersion", ""),
            "age_rating": d.get("trackContentRating", ""),
            "apple_primary_genre": d.get("primaryGenreName", ""),
            "all_genres": "; ".join(d.get("genres", [])),
            "seller_name": d.get("sellerName", ""),
            "seller_url": d.get("sellerUrl", ""),
            "languages_count": len(d.get("languageCodesISO2A", [])),
            # Content advisories (e.g. "Infrequent/Mild Cartoon Violence"), NOT an IAP flag
            "advisories": "; ".join(d.get("advisories", [])),
            "description": d.get("description", "") if INCLUDE_DESCRIPTIONS else "",
        })

    if missing:
        log.warning("%d rows had no Lookup result (enrichment columns left blank; "
                    "%d lookup batches failed outright)", missing, failed_batches)
    return missing


# --------------------------------------------------------------------------
# Step 3: output
# --------------------------------------------------------------------------
def build_columns():
    rank_cols = (["best_rank", "chart_count", "chart_appearances"]
                 if DEDUPE_MODE == "merge" else ["rank"])
    cols = (["app_name", "app_id", "type", "category"] + rank_cols +
            ["developer", "developer_url", "bundle_id", "app_url", "feed_release_date",
             "copyright", "icon_url", "summary", "snapshot_date"])
    if ENRICH:
        cols += EXTRA_COLS
    if not INCLUDE_DESCRIPTIONS:
        cols = [c for c in cols if c not in ("summary", "description")]
    return cols


def safe_csv_cell(v):
    """Neutralize spreadsheet formula injection in CSV output."""
    if isinstance(v, str) and re.match(r"^[=+\-@\t\r]", v):
        return "'" + v
    return v


def write_csv(rows, cols):
    tmp = OUTPUT_FILE + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: safe_csv_cell(r.get(c, "")) for c in cols})
    os.replace(tmp, OUTPUT_FILE)  # atomic: never leaves a half-written file
    log.info("Wrote %d rows, %d columns to %s", len(rows), len(cols), OUTPUT_FILE)


def write_to_sheet(rows, cols):
    import gspread

    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if sa_json:
        # Unattended mode (e.g. GitHub Actions): service account, no sign-in popup.
        # The spreadsheet must be shared with the service account's email as Editor.
        if not SPREADSHEET_URL:
            raise RuntimeError("Set the SPREADSHEET_URL env var when using a service account "
                               "(it can't create or find a sheet in your Drive).")
        gc = gspread.service_account_from_dict(json.loads(sa_json))
    else:
        from google.colab import auth
        from google.auth import default
        auth.authenticate_user()  # pops up a Google sign-in prompt
        creds, _ = default()
        gc = gspread.authorize(creds)

    created = False
    if SPREADSHEET_URL:
        sh = gc.open_by_url(SPREADSHEET_URL)
    else:
        try:
            sh = gc.open(SPREADSHEET_TITLE)   # reuse the existing spreadsheet
        except gspread.SpreadsheetNotFound:
            sh = gc.create(SPREADSHEET_TITLE)
            created = True
            log.info("Created new spreadsheet. Save this URL: %s", sh.url)

    values = [cols] + [[r.get(c, "") for c in cols] for r in rows]

    try:
        ws = sh.worksheet(WORKSHEET_NAME)     # same-day rerun: overwrite today's tab
        ws.clear()
        ws.resize(rows=len(values), cols=len(cols))
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(WORKSHEET_NAME, rows=len(values), cols=len(cols))

    if created:
        # A brand-new spreadsheet starts with an empty "Sheet1"; remove it
        try:
            sh.del_worksheet(sh.worksheet("Sheet1"))
        except Exception:
            pass

    # Upload in chunks so no single request gets too large; retry each chunk
    CHUNK = 500
    for i in range(0, len(values), CHUNK):
        for attempt in range(MAX_RETRIES):
            try:
                # RAW = text stays text, so names starting with "=" can't run as formulas
                ws.update(values=values[i:i + CHUNK], range_name=f"A{i + 1}",
                          value_input_option="RAW")
                break
            except Exception as e:
                if attempt == MAX_RETRIES - 1:
                    raise
                delay = 2 ** attempt + random.random()
                log.warning("sheet write retry %d (%s), sleeping %.1fs", attempt + 1, e, delay)
                time.sleep(delay)
        time.sleep(1)

    ws.freeze(rows=1)
    ws.format("1:1", {"textFormat": {"bold": True}})
    log.info("Wrote %d rows to Google Sheet: %s", len(rows), sh.url)
    return sh.url


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    rows, failed, short = collect_charts()
    if not rows:
        raise RuntimeError("No chart data was collected; nothing to write.")

    for r in rows:
        r["snapshot_date"] = SNAPSHOT_DATE

    if DEDUPE_MODE == "merge":
        before = len(rows)
        rows = merge_duplicates(rows)
        log.info("Merged %d chart entries into %d unique apps", before, len(rows))

    missing_lookup = enrich(rows) if ENRICH else 0

    cols = build_columns()
    write_csv(rows, cols)  # always write the local backup first

    sheet_url = None
    if WRITE_TO_SHEET:
        try:
            sheet_url = write_to_sheet(rows, cols)
        except Exception as e:
            log.error("Google Sheets write failed (CSV backup is still saved): %s", e)

    # ---- Summary -----------------------------------------------------------
    print("\n" + "=" * 60)
    print(f"Snapshot {SNAPSHOT_DATE} | {COUNTRY.upper()} | {CHART} | limit {LIMIT}")
    print(f"Charts requested: {len(JOBS)} | failed: {len(failed)} | short: {len(short)}")
    print(f"Rows written: {len(rows)} | rows without Lookup data: {missing_lookup}")
    for label, err in failed:
        print(f"  FAILED: {label} -> {err}")
    for label, n in short:
        print(f"  SHORT:  {label} -> {n} entries")
    if failed:
        print("Rerun the script: charts that succeeded are cached, so only the failed ones refetch.")
    if sheet_url:
        print(f"Sheet: {sheet_url}")
    print("=" * 60)

    # In CI (GitHub sets CI=true), fail the run so you get an email notification
    if os.environ.get("CI") and (failed or (WRITE_TO_SHEET and not sheet_url)):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

# Top iOS Apps Tracker

Every day, this project pulls the **top 200 free iPhone apps (US App Store)** for every app category and every game subcategory, enriches each app with extra details, and writes the result to a **Google Sheet** (one new tab per day) plus a **CSV backup**. It also flags **newly added apps** and shows **rank movement within each category** compared with the previous snapshot.

It runs automatically on **GitHub Actions**, so once it's set up there is nothing to do day to day.

---

## What you get

- **40 charts per run:** 24 app categories and 16 game subcategories, 200 apps each (Apple's feed maximum).
- **One row per app** (merge mode, the default). Apps that appear in several charts are combined into a single row.
- **A new Google Sheet tab each day**, named `top100_YYYY-MM-DD` (UTC date). A rerun on the same UTC day overwrites that day's tab.
- **A CSV backup** (`ios_top100_all_categories.csv`), saved as a downloadable artifact on every workflow run for 30 days.
- **Change tracking** against the most recent earlier tab in the sheet.

---

## Repository layout

```
top_ios_apps/
├── ios_top100.py              # the script
├── requirements.txt           # gspread, google-auth
├── README.md
└── .github/
    └── workflows/
        └── daily.yml          # the daily schedule
```

---

## How it works

1. **Fetch charts.** Pulls Apple's RSS top-free chart for each genre, with retries, exponential backoff and a same-day on-disk cache.
2. **Enrich.** Looks up each unique app in Apple's Lookup API for ratings, version, seller, languages and more.
3. **Compare.** Reads the latest earlier `top100_*` tab from your Google Sheet and works out `newly_added` and `rank_change`.
4. **Write.** Saves the CSV first, then writes the Google Sheet tab.
5. **Summarize.** Prints a run summary: failed charts, short charts, rows without Lookup data, and how many apps are new.

---

## Columns

| Column | Meaning |
|---|---|
| `app_name`, `app_id`, `bundle_id`, `app_url`, `icon_url` | App identity and links |
| `type` | `Apps`, `Games`, or both, comma-joined |
| `category` | Every category chart the app appears in, best-ranked first |
| `best_rank` | Its best rank across all charts |
| `chart_count` | Number of charts it appears in |
| `chart_appearances` | e.g. `Apps > Utilities #3; Games > Puzzle #12` |
| `newly_added` | `NEW` if the app was in none of the previous snapshot's charts, otherwise blank |
| `rank_change` | Movement within each category chart (see below) |
| `developer`, `developer_url`, `copyright`, `feed_release_date` | From the RSS feed |
| `snapshot_date` | The UTC date of the run |
| `avg_rating`, `rating_count`, `version`, `current_version_release_date`, `original_release_date`, `min_ios_version`, `age_rating`, `apple_primary_genre`, `all_genres`, `seller_name`, `seller_url`, `languages_count`, `advisories` | From the Lookup API |

`advisories` lists content advisories (for example mild cartoon violence). It is **not** an in-app-purchase flag.

### Reading `rank_change`

`rank_change` is the previous rank minus the current rank, so **a positive number means the app moved up**.

```
Apps > Utilities +3; Games > Puzzle -2; Games > Word NEW; Apps > News 0
```

- `+3`: moved up three places in that category
- `-2`: dropped two places
- `0`: unchanged
- `NEW`: wasn't in that category's chart in the previous snapshot

An app can be `NEW` in one category without being `newly_added`, if it was already charting in another category.

**Blank change columns** mean there was no earlier tab to compare against (the first run) or the previous tab couldn't be read.

---

## Setup

### 1. Add the files

Put `ios_top100.py`, `requirements.txt` and `.github/workflows/daily.yml` in the repo.

`requirements.txt`:

```
gspread>=6.0
google-auth>=2.0
```

### 2. Google service account (one time)

1. In [Google Cloud Console](https://console.cloud.google.com/), create or pick a project.
2. Under **APIs & Services → Library**, enable the **Google Sheets API** and the **Google Drive API**.
3. Under **IAM & Admin → Service Accounts**, create a service account (no roles needed).
4. Open it, go to **Keys → Add key → Create new key → JSON**, and download the file.
5. Create a spreadsheet in Google Sheets.
6. Click **Share** on that spreadsheet and add the service account's email (`name@project.iam.gserviceaccount.com`) as **Editor**. This step is required.
7. Copy the spreadsheet's URL.

### 3. GitHub secrets

In the repo, go to **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | The full contents of the downloaded JSON key |
| `SPREADSHEET_URL` | The spreadsheet URL |

Never commit the JSON key to the repo.

### 4. Test

Go to **Actions → Daily iOS Top 100 → Run workflow**, then check the log summary, the new tab in your sheet, and the CSV under the run's **Artifacts**.

---

## Schedule

The schedule is set in `.github/workflows/daily.yml`:

```yaml
on:
  schedule:
    - cron: "17 11 * * *"   # 11:17 UTC daily
  workflow_dispatch:         # manual "Run workflow" button
```

- Cron times are **UTC** and don't shift for daylight saving. 11:17 UTC is 6:17 AM US Eastern in winter and 7:17 AM in summer.
- GitHub can start scheduled runs a few minutes late.
- Scheduled runs only trigger from the repo's default branch.
- GitHub pauses scheduled workflows after **60 days without repo activity** in public repos. Any commit, or re-enabling the workflow in the **Actions** tab, restarts it.
- The script assumes **one run per UTC day**. Extra runs on the same UTC day overwrite that day's tab, and change tracking compares against the latest tab with an *earlier* date.

---

## Configuration

Settings are constants near the top of `ios_top100.py`:

| Setting | Default | Notes |
|---|---|---|
| `CHART` | `topfreeapplications` | Or `toppaidapplications`, `topgrossingapplications` |
| `LIMIT` | `200` | Apps per chart. Apple's feed caps at 200 |
| `COUNTRY` | `us` | App Store country code |
| `ENRICH` | `True` | `False` skips the Lookup API (faster, fewer columns) |
| `INCLUDE_DESCRIPTIONS` | `False` | Adds long description text; makes output very large |
| `DEDUPE_MODE` | `"merge"` | `"separate"` gives one row per app per chart, with a numeric `rank_change` |
| `TRACK_CHANGES` | `True` | Turns `newly_added` / `rank_change` on or off |
| `MAX_WORKERS` | `3` | Lower it if Apple rate-limits the runner |

The spreadsheet is chosen from the `SPREADSHEET_URL` environment variable, and in a service-account run it is required.

Running in Google Colab also works. If `GOOGLE_SERVICE_ACCOUNT_JSON` isn't set, the script falls back to a Colab sign-in prompt, and it can find or create a spreadsheet titled "iOS Top 100 - All Categories".

---

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| **Exit code 2** | Python couldn't find the script. Check that the workflow's `run: python ios_top100.py` matches the real filename and path (case-sensitive). |
| **Run fails with "Set the SPREADSHEET_URL env var"** | The `SPREADSHEET_URL` secret is missing or misnamed. |
| **Run fails or the sheet isn't written, with a permissions error** | The spreadsheet isn't shared with the service account's email as Editor, or the Sheets/Drive APIs aren't enabled. |
| **`FAILED` charts in the summary** | Apple may throttle GitHub's shared IPs. Re-run the workflow, or lower `MAX_WORKERS`. The run exits non-zero in CI so you get an email. |
| **Apps flagged `NEW` that aren't new** | A chart failed in the previous run, so those apps were missing from yesterday's baseline. Re-run the failed day. |
| **Change columns are blank** | No earlier tab exists yet (first run), or the sheet couldn't be read. Check the log for a warning. |
| **Two tests on one day leave one tab** | Expected. Tabs are named by UTC date, so the second run overwrites the first. |

---

## Notes and limits

- **Sheet size:** each daily tab has one row per unique app and about 30 columns. Google Sheets caps a spreadsheet at 10 million cells, so divide 10,000,000 by (rows in a tab × 30) to see how many days you have. At 200 apps per chart that can be only a couple of months, so archive old tabs or start a new spreadsheet before then.
- **Cache:** the on-disk cache lives in `/tmp` and doesn't persist between GitHub runs. Retries within a single run still apply.
- **Data source:** the chart data comes from Apple's public iTunes RSS feed and Lookup API. Charts are US top free and reflect Apple's own ranking.
- **Naming:** tabs and the CSV keep the `top100` name even though the depth is now 200. Change tracking finds the previous tab by that name, so don't rename it.
- **Changing `LIMIT`:** on the first run after changing the depth, apps in the newly added ranks are flagged `NEW` because yesterday's tab didn't include them. The next day's run is accurate.
- **Formula safety:** the sheet is written in RAW mode, and CSV cells that start with `=`, `+`, `-` or `@` are neutralized, so app names can't run as formulas.

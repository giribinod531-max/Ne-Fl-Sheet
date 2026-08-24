# Ne-Fl-Sheet — validated one-day test

This package is Stage 1 of the NEPSE floorsheet cloud downloader. It is limited
to one selected date so the source and output can be checked before a monthly
or historical run is enabled.

## What it validates

For the selected date, the program:

1. opens the public Merolagani floorsheet;
2. puts the date into the website's date field and searches it;
3. downloads every advertised page;
4. verifies the returned market date;
5. rejects duplicate transaction numbers;
6. compares downloaded rows, quantity and amount with the site's displayed
   daily totals;
7. records every page that returned fewer rows than advertised.

The result is never silently called complete.

| Status | Meaning | Saved location |
|---|---|---|
| `COMPLETE` | Rows, quantity and amount all match | `output/daily_csv/` |
| `MINOR_GAP` | Every difference is no more than 0.10% | `output/daily_csv/` |
| `REJECT` | Wrong date, unsafe discrepancy or another validation failure | `output/rejected_csv/` when rows are available |
| `NOT_AVAILABLE` | No data was shown for that date | No daily CSV |
| `FAILED` | Website/network/format failure | No daily CSV |

`MINOR_GAP` is usable for initial descriptive review, but it must remain marked
and should not be mixed into a future machine-learning training set unless we
make an explicit rule for it. Start future modelling with `COMPLETE` dates.

## First GitHub test

1. Upload all extracted package contents to the root of your GitHub repository.
   The repository must show `floorsheet_downloader.py`, `requirements.txt`,
   `tests`, and `.github` at the top level.
2. Open the repository's **Actions** tab.
3. Select **01 - Test One Trading Day**.
4. Press **Run workflow**.
5. Keep the date as `2026-08-21` and press the green **Run workflow** button.
6. When the run finishes, open it and download the
   `floorsheet-test-2026-08-21` artifact.

Expected for the known sample: the program should create the visible daily CSV
and label it `MINOR_GAP`. The quality report should clearly show the difference
between the advertised totals and the downloaded rows.

Do not start a 2020-to-current run yet. After this one-day output is reviewed,
Stage 2 will add one-month jobs and resumable historical backfill controls.

The code contains no password, token or private analysis rule.

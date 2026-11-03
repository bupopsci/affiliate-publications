# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Generates a static publications page for Boston University Population Science Seminar Series (BU PopSci) organizers and affiliates. The page is built from the faculty's public ORCID profiles and deployed to GitHub Pages (`bupopsci/affiliate-publications`).

## Commands

```bash
python -m pip install -r requirements.txt   # only dependency: requests
python scripts/build_publications.py        # writes docs/faculty.html and docs/index.html
```

- Run the script from the repo root. The paths `data/faculty.csv` and `docs/` are relative to the working directory.
- The build needs the ORCID API credentials `ORCID_CLIENT_ID` and `ORCID_CLIENT_SECRET` set as environment variables. In CI they come from GitHub repository secrets. Without them the script exits with an error before fetching anything.
- The repo has no tests, linter or formatter configured.

## Architecture

Everything happens in one script, `scripts/build_publications.py`, in this order:

1. **`load_roster()`:** reads `data/faculty.csv`, which must have the column headers `Name` and `ORCID`. `clean_orcid()` normalizes each iD (strips any `https://orcid.org/` prefix and checks the checksum). Rows with an invalid iD are skipped with a printed warning, not treated as an error.
2. **`get_access_token()`:** gets a `/read-public` token from ORCID using the client-credentials grant.
3. **`fetch_works()`:** calls `GET https://pub.orcid.org/v3.0/{orcid}/works`. It keeps only the first `work-summary` in each group, which is ORCID's preferred copy. Each work becomes a dict with the keys `title`, `year`, `journal`, `doi`, `type` and `new`. `type` is a readable label made by `type_label()`, and `new` means the work was added to ORCID within `NEW_WINDOW_DAYS`. The parsing is deliberately defensive about the shape of the ORCID JSON.
4. **Deduplication in `main()`:** works are keyed by `doi:<lowercased doi>`, or `title:<casefolded, whitespace-normalized title>|year:<year>` when there is no DOI. Each unique work keeps a `people` set of **ORCID iDs**, not names. The `names` dict maps each iD to its roster entry (`name` is "Last, First" for sorting, `display` is "First Last" for showing).
5. **Rendering:** the page skeleton is an f-string in `main()`. The CSS and JS live in the module constants `PAGE_CSS` and `PAGE_JS`, which are plain strings, so their braces are *not* doubled. Keep it that way, and don't move CSS or JS back into the f-string. All interpolated text, including `data-*` attributes, goes through `safe()` (`html.escape`). `publication_card()` renders each entry. Works with no 4-digit year sort last, under "Date unknown".
6. **Client-side filtering (`PAGE_JS`):** filtering reads each card's `data-people` (iDs separated by spaces), `data-year` (`unknown` when the year is missing), `data-type` and `data-text` (lowercase text to search). Filters cover only `#by-year`. The "Newest" section is hidden while any filter is on, so cards don't show twice. Filter state is kept in the URL hash (`#faculty=<id>,<id>&year=&type=&q=`). Year jump links use `data-jump` and scroll with JS so they don't overwrite that hash.

Failure policy: if one person's fetch fails, the script logs it and moves on. The build fails only when the roster is empty, the credentials are missing, or every fetch fails.

## Deployment

`.github/workflows/publish.yml` builds on Python 3.12 and deploys `docs/` with `actions/deploy-pages`. It runs:

- weekly, on Mondays at 11:00 UTC
- on manual dispatch
- on pushes that change `data/faculty.csv`, the script, `requirements.txt` or the workflow

`docs/` is generated output and is not committed. Adding or removing a faculty member only means editing `data/faculty.csv`.

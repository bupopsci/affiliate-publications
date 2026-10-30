import csv
import html
import os
import re
import sys
import traceback
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

import requests


ROSTER = Path("data/faculty.csv")
OUTPUT = Path("docs/faculty.html")
INDEX = Path("docs/index.html")
API_ROOT = "https://pub.orcid.org/v3.0"


def clean_orcid(value):
    """Return a valid, hyphenated ORCID iD, or None."""
    value = (value or "").strip()
    value = re.sub(r"^https?://orcid\.org/", "", value, flags=re.I)
    compact = re.sub(r"[^0-9Xx]", "", value).upper()

    if not re.fullmatch(r"\d{15}[\dX]", compact):
        return None

    # Check the ORCID checksum.
    total = 0
    for digit in compact[:15]:
        total = (total + int(digit)) * 2

    check = (12 - total % 11) % 11
    expected = "X" if check == 10 else str(check)

    if compact[-1] != expected:
        return None

    # The API expects the ORCID iD with hyphens.
    return f"{compact[:4]}-{compact[4:8]}-{compact[8:12]}-{compact[12:]}"


def get_text(value):
    """Extract text from an ORCID value object or a plain string."""
    if isinstance(value, str):
        return value.strip()

    if isinstance(value, dict):
        value = value.get("value", "")
        if isinstance(value, str):
            return value.strip()

    return ""


def get_doi(work):
    """Get a DOI from an ORCID work record, if one is available."""
    if not isinstance(work, dict):
        return ""

    external_ids = work.get("external-ids") or {}

    if isinstance(external_ids, dict):
        items = external_ids.get("external-id", [])
    elif isinstance(external_ids, list):
        items = external_ids
    else:
        items = []

    if not isinstance(items, list):
        return ""

    for item in items:
        if not isinstance(item, dict):
            continue

        id_type = get_text(item.get("external-id-type")).lower()
        if id_type == "doi":
            doi = get_text(item.get("external-id-value"))
            if doi:
                doi = re.sub(
                    r"^https?://doi\.org/",
                    "",
                    doi,
                    flags=re.I,
                )
                return doi.strip()

    return ""


def load_roster():
    """Read and validate faculty names and ORCID iDs from the CSV roster."""
    if not ROSTER.exists():
        raise FileNotFoundError(f"Roster not found: {ROSTER}")

    people = []

    with ROSTER.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)

        if not reader.fieldnames or not {"Name", "ORCID"}.issubset(
            set(reader.fieldnames)
        ):
            raise ValueError(
                "The roster must have the column headers Name and ORCID."
            )

        for row in reader:
            name = (row.get("Name") or "").strip()
            raw_orcid = row.get("ORCID") or ""
            orcid = clean_orcid(raw_orcid)

            if not name:
                continue

            if not orcid:
                print(f"Skipping invalid or missing ORCID for: {name}")
                continue

            people.append({"name": name, "orcid": orcid})

    return people


def get_access_token():
    """Get a public-read access token using GitHub Actions secrets."""
    client_id = os.environ.get("ORCID_CLIENT_ID")
    client_secret = os.environ.get("ORCID_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise RuntimeError(
            "ORCID GitHub secrets are missing. "
            "Check ORCID_CLIENT_ID and ORCID_CLIENT_SECRET in repository settings."
        )

    response = requests.post(
        "https://orcid.org/oauth/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
            "scope": "/read-public",
        },
        headers={"Accept": "application/json"},
        timeout=30,
    )
    response.raise_for_status()

    token_data = response.json()
    token = token_data.get("access_token")

    if not token:
        raise RuntimeError("ORCID did not return an access token.")

    return token


def fetch_works(person, token):
    """Retrieve public works for one person from the ORCID API."""
    url = f"{API_ROOT}/{person['orcid']}/works"

    response = requests.get(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
        timeout=30,
    )
    response.raise_for_status()

    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("ORCID returned an unexpected response format.")

    groups = payload.get("group", [])
    if not isinstance(groups, list):
        groups = []

    works = []

    for group in groups:
        if not isinstance(group, dict):
            continue

        summaries = group.get("work-summary", [])
        if not isinstance(summaries, list):
            continue

        for summary in summaries:
            if not isinstance(summary, dict):
                continue

            title_data = summary.get("title")
            if isinstance(title_data, dict):
                title_data = title_data.get("title")

            title = get_text(title_data)
            if not title:
                continue

            date = summary.get("publication-date") or {}
            if not isinstance(date, dict):
                date = {}

            year = get_text(date.get("year"))
            journal = get_text(summary.get("journal-title"))
            doi = get_doi(summary)

            works.append(
                {
                    "title": title,
                    "year": year,
                    "journal": journal,
                    "doi": doi,
                    "person": person["name"],
                }
            )

    return works


def safe(value):
    """Escape text for safe inclusion in HTML."""
    return html.escape(str(value), quote=True)


def publication_card(work):
    """Create one HTML card for a publication."""
    title = safe(work["title"])
    year = safe(work["year"] or "Date unknown")
    journal = safe(work["journal"])
    people = safe(", ".join(sorted(work["people"])))

    if work["doi"]:
        doi = quote(work["doi"], safe="/():._-")
        title_html = (
            f'<a href="https://doi.org/{safe(doi)}" '
            f'target="_blank" rel="noopener">{title}</a>'
        )
    else:
        title_html = title

    details = " · ".join(part for part in (journal, year) if part)

    return (
        '<article class="publication">'
        f"<h3>{title_html}</h3>"
        f"<p>{details}</p>"
        f'<p class="authors">BU PopSci faculty: {people}</p>'
        "</article>"
    )


def main():
    people = load_roster()

    if not people:
        raise RuntimeError(
            "No faculty with valid ORCID iDs were found in data/faculty.csv."
        )

    token = get_access_token()
    publications = {}
    successful_requests = 0

    for person in people:
        try:
            works = fetch_works(person, token)
            successful_requests += 1
            print(f"Retrieved {len(works)} works for {person['name']}.")
        except requests.RequestException as error:
            print(
                f"Could not retrieve works for {person['name']} "
                f"(ORCID {person['orcid']}): {error}"
            )
            continue
        except (ValueError, TypeError, AttributeError) as error:
            print(
                f"Could not read works for {person['name']} "
                f"(ORCID {person['orcid']}): {error}"
            )
            continue

        for work in works:
            doi = work["doi"].strip().lower()

            if doi:
                key = f"doi:{doi}"
            else:
                normalized_title = " ".join(
                    work["title"].casefold().split()
                )
                key = f"title:{normalized_title}|year:{work['year']}"

            if key not in publications:
                publications[key] = {
                    **work,
                    "people": set(),
                }

            publications[key]["people"].add(person["name"])

    if successful_requests == 0:
        raise RuntimeError(
            "Could not retrieve works for any faculty member. "
            "Check the ORCID IDs and the API request errors above."
        )

    all_works = list(publications.values())

    def sort_key(work):
        year = work["year"]
        year_number = int(year) if re.fullmatch(r"\d{4}", year or "") else 0
        return year_number, work["title"].casefold()

    all_works.sort(key=sort_key, reverse=True)

    counts = defaultdict(int)
    for work in all_works:
        for name in work["people"]:
            counts[name] += 1

    newest = "".join(
        publication_card(work) for work in all_works[:10]
    )

    years = defaultdict(list)
    for work in all_works:
        years[work["year"] or "Date unknown"].append(work)

    year_sections = []
    for year in sorted(
        years,
        key=lambda value: int(value) if re.fullmatch(r"\d{4}", value) else 0,
        reverse=True,
    ):
        cards = "".join(
            publication_card(work) for work in years[year]
        )
        year_sections.append(
            f"<section><h2>{safe(year)}</h2>{cards}</section>"
        )

    researcher_list = "".join(
        f"<li>{safe(person['name'])} — "
        f"{counts[person['name']]} publications</li>"
        for person in people
    )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BU PopSci Faculty Publications</title>
<style>
body {{ max-width: 960px; margin: 2rem auto; padding: 0 1rem;
       font: 16px/1.5 Arial, sans-serif; color: #222; }}
h1, h2 {{ color: #153e5c; }}
input {{ box-sizing: border-box; width: 100%; padding: .7rem;
         margin: 1rem 0; font-size: 1rem; }}
.publication {{ border-bottom: 1px solid #ddd; padding: .4rem 0; }}
.publication h3 {{ margin-bottom: .2rem; }}
.publication p {{ margin: .2rem 0; }}
.authors {{ color: #555; font-size: .9rem; }}
a {{ color: #145b8c; }}
</style>
</head>
<body>
<h1>BU PopSci Faculty Publications</h1>
<p>Publication records retrieved from publicly available ORCID profiles.</p>

<h2>Faculty</h2>
<ul>{researcher_list}</ul>

<label for="search">Search publications</label>
<input id="search" type="search"
       placeholder="Search titles, journals, or faculty">

<h2>Newest publications</h2>
<div id="newest">
{newest or "<p>No public works were found.</p>"}
</div>

<h2>Publications by year</h2>
<div id="by-year">
{''.join(year_sections) or "<p>No public works were found.</p>"}
</div>

<script>
document.getElementById("search").addEventListener("input", function () {{
  const query = this.value.toLowerCase();
  document.querySelectorAll(".publication").forEach(function (item) {{
    item.hidden = !item.innerText.toLowerCase().includes(query);
  }});
}});
</script>
</body>
</html>
"""

    OUTPUT.write_text(page, encoding="utf-8")
    INDEX.write_text(
        '<!doctype html><meta http-equiv="refresh" '
        'content="0; url=faculty.html">'
        '<a href="faculty.html">BU PopSci Faculty Publications</a>',
        encoding="utf-8",
    )

    print(
        f"Wrote {OUTPUT} with "
        f"{len(all_works)} unique publications."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Build failed: {error}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)

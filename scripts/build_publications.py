import csv
import html
import os
import re
import sys
import traceback
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests


ROSTER = Path("data/faculty.csv")
OUTPUT = Path("docs/faculty.html")
INDEX = Path("docs/index.html")
API_ROOT = "https://pub.orcid.org/v3.0"

TYPE_LABELS = {
    "journal-article": "Journal article",
    "preprint": "Preprint",
    "book": "Book",
    "book-chapter": "Book chapter",
    "edited-book": "Edited book",
    "conference-paper": "Conference paper",
    "conference-abstract": "Conference abstract",
    "conference-poster": "Conference poster",
    "dissertation-thesis": "Dissertation",
    "report": "Report",
    "working-paper": "Working paper",
    "review": "Review",
    "data-set": "Dataset",
    "book-review": "Book review",
    "standards-and-policy": "Standards and policy",
    "other": "Other",
}


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


def display_name(name):
    """Turn a roster name like 'Stokes, Andrew' into 'Andrew Stokes'."""
    if "," in name:
        last, first = (part.strip() for part in name.split(",", 1))
        if first:
            return f"{first} {last}"
    return name.strip()


def type_label(work_type):
    """Turn an ORCID work type into a readable label."""
    work_type = work_type.strip().lower() if isinstance(work_type, str) else ""
    if not work_type:
        return "Other"
    return TYPE_LABELS.get(
        work_type, work_type.replace("-", " ").replace("_", " ").capitalize()
    )


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

            people.append(
                {"name": name, "display": display_name(name), "orcid": orcid}
            )

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

        # Each group holds copies of the same work from different sources.
        # The first summary is ORCID's preferred copy.
        summary = next((s for s in summaries if isinstance(s, dict)), None)
        if summary is None:
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

        works.append(
            {
                "title": title,
                "year": get_text(date.get("year")),
                "journal": get_text(summary.get("journal-title")),
                "doi": get_doi(summary),
                "type": type_label(summary.get("type")),
            }
        )

    return works


def safe(value):
    """Escape text for safe inclusion in HTML."""
    return html.escape(str(value), quote=True)


def year_key(value):
    """Sort key that puts 4-digit years first, newest first, unknown last."""
    return int(value) if re.fullmatch(r"\d{4}", value or "") else 0


def publication_card(work, names):
    """Create one HTML card for a publication."""
    title = safe(work["title"])
    year = work["year"] or "Date unknown"
    orcids = sorted(work["people"], key=lambda orcid: names[orcid]["name"])

    if work["doi"]:
        doi = quote(work["doi"], safe="/():._-")
        doi_url = f"https://doi.org/{safe(doi)}"
        title_html = (
            f'<a href="{doi_url}" target="_blank" rel="noopener">{title}</a>'
        )
        doi_html = (
            f'<p class="doi">DOI: <a href="{doi_url}" target="_blank" '
            f'rel="noopener">{safe(work["doi"])}</a></p>'
        )
    else:
        title_html = title
        doi_html = ""

    details = " · ".join(
        safe(part) for part in (work["journal"], year) if part
    )

    badges = f'<span class="badge">{safe(work["type"])}</span>'

    people_html = "".join(
        f'<button type="button" class="person" '
        f'data-orcid="{safe(orcid)}" '
        f'title="Show only {safe(names[orcid]["display"])}">'
        f'{safe(names[orcid]["display"])}</button>'
        for orcid in orcids
    )

    search_text = " ".join(
        [work["title"], work["journal"], year, work["type"], work["doi"]]
        + [names[orcid]["display"] for orcid in orcids]
    ).lower()

    return (
        f'<article class="publication" '
        f'data-people="{safe(" ".join(orcids))}" '
        f'data-year="{safe(work["year"] or "unknown")}" '
        f'data-type="{safe(work["type"])}" '
        f'data-text="{safe(search_text)}">'
        f'<div class="badges">{badges}</div>'
        f"<h3>{title_html}</h3>"
        f'<p class="meta">{details}</p>'
        f'<p class="authors">{people_html}</p>'
        f"{doi_html}"
        "</article>"
    )


PAGE_CSS = """
:root {
  --bg: #f6f7f9;
  --surface: #ffffff;
  --text: #1f2933;
  --muted: #5f6b7a;
  --border: #dde2e8;
  --accent: #153e5c;
  --link: #145b8c;
  --chip-bg: #eef2f6;
  --chip-on-bg: #153e5c;
  --chip-on-text: #ffffff;
  --badge-bg: #e8eef4;
  --header-bg: #153e5c;
  --header-text: #ffffff;
  --orcid: #4b7c12;
  --shadow: 0 1px 2px rgba(16, 24, 40, .06);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #11161c;
    --surface: #1a2129;
    --text: #e5e9ee;
    --muted: #9aa6b4;
    --border: #2c3640;
    --accent: #9cc3e4;
    --link: #8cc4f0;
    --chip-bg: #242d37;
    --chip-on-bg: #9cc3e4;
    --chip-on-text: #11161c;
    --badge-bg: #26313c;
    --header-bg: #0b2235;
    --header-text: #ffffff;
    --orcid: #a6ce39;
    --shadow: none;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #11161c;
  --surface: #1a2129;
  --text: #e5e9ee;
  --muted: #9aa6b4;
  --border: #2c3640;
  --accent: #9cc3e4;
  --link: #8cc4f0;
  --chip-bg: #242d37;
  --chip-on-bg: #9cc3e4;
  --chip-on-text: #11161c;
  --badge-bg: #26313c;
  --header-bg: #0b2235;
  --header-text: #ffffff;
  --orcid: #a6ce39;
  --shadow: none;
  color-scheme: dark;
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
body::before {
  content: "";
  position: fixed;
  inset: 0 0 auto;
  height: env(safe-area-inset-top, 0px);
  z-index: 10;
  background: var(--bg);
}
html { scroll-behavior: smooth; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 16px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
}
a { color: var(--link); }
:focus-visible { outline: 3px solid var(--link); outline-offset: 2px; }
.wrap {
  max-width: 1000px;
  margin: 0 auto;
  padding-left: max(16px, env(safe-area-inset-left, 0px));
  padding-right: max(16px, env(safe-area-inset-right, 0px));
}

header.site {
  background: var(--header-bg);
  color: var(--header-text);
  padding-block: 2rem 1.5rem;
}
header.site h1 { margin: 0 0 .35rem; font-size: 1.9rem; line-height: 1.2; }
header.site p { margin: .2rem 0; opacity: .9; }
.stats { font-weight: 600; }

main { padding: 1.5rem 0 4rem; }
h2 { color: var(--accent); margin: 2rem 0 .75rem; font-size: 1.35rem; }

.panel {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  box-shadow: var(--shadow);
  padding: .25rem 1rem;
}
.panel > summary {
  cursor: pointer;
  font-weight: 700;
  color: var(--accent);
  padding: .65rem 0;
  list-style-position: inside;
}
.panel-help { color: var(--muted); font-size: .9rem; margin: 0 0 .75rem; }
.chips {
  list-style: none;
  margin: 0 0 .75rem;
  padding: 0;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: .5rem;
}
.chip-item { display: flex; align-items: stretch; }
.chip {
  flex: 1;
  display: flex;
  justify-content: space-between;
  gap: .5rem;
  text-align: left;
  font: inherit;
  font-size: .92rem;
  color: var(--text);
  background: var(--chip-bg);
  border: 1px solid var(--border);
  border-right: 0;
  border-radius: 999px 0 0 999px;
  padding: .4rem .5rem .4rem .9rem;
  cursor: pointer;
}
.chip:hover { border-color: var(--link); }
.chip[aria-pressed="true"] {
  background: var(--chip-on-bg);
  color: var(--chip-on-text);
  border-color: var(--chip-on-bg);
}
.chip .count { opacity: .75; font-variant-numeric: tabular-nums; }
.orcid {
  display: flex;
  align-items: center;
  font-size: .75rem;
  font-weight: 700;
  text-decoration: none;
  color: var(--orcid);
  background: var(--chip-bg);
  border: 1px solid var(--border);
  border-radius: 0 999px 999px 0;
  padding: 0 .65rem;
}
.orcid:hover { border-color: var(--link); }
.panel-actions { margin: 0 0 .75rem; }

.btn-link {
  font: inherit;
  font-size: .9rem;
  color: var(--link);
  background: none;
  border: 0;
  padding: 0;
  cursor: pointer;
  text-decoration: underline;
}

.toolbar {
  position: sticky;
  top: env(safe-area-inset-top, 0px);
  z-index: 10;
  background: var(--bg);
  border-bottom: 1px solid var(--border);
  padding: .75rem 0;
  margin-top: 1rem;
  display: flex;
  flex-wrap: wrap;
  gap: .5rem .75rem;
  align-items: center;
}
.toolbar input, .toolbar select {
  font: inherit;
  color: var(--text);
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: .5rem .7rem;
}
.toolbar input { flex: 1 1 260px; min-width: 0; }
.toolbar .status { color: var(--muted); font-size: .9rem; margin-left: auto; }
.sr-only {
  position: absolute; width: 1px; height: 1px; overflow: hidden;
  clip: rect(0 0 0 0); white-space: nowrap;
}

.year-nav {
  display: flex;
  flex-wrap: wrap;
  gap: .35rem;
  margin: .75rem 0 0;
  font-size: .88rem;
}
.year-nav a {
  text-decoration: none;
  padding: .15rem .55rem;
  border-radius: 6px;
  background: var(--chip-bg);
}
.year-nav a:hover { text-decoration: underline; }

.year-section { scroll-margin-top: 90px; }
.year-section h2 {
  border-bottom: 2px solid var(--border);
  padding-bottom: .3rem;
}
.publication {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  box-shadow: var(--shadow);
  padding: .85rem 1rem;
  margin: 0 0 .65rem;
}
.publication:hover { border-color: var(--link); }
.publication h3 { margin: .25rem 0; font-size: 1.05rem; line-height: 1.4; }
.publication h3 a { text-decoration: none; }
.publication h3 a:hover { text-decoration: underline; }
.publication p { margin: .2rem 0; }
.meta { color: var(--muted); font-size: .92rem; font-style: italic; }
.badges { display: flex; gap: .35rem; }
.badge {
  font-size: .72rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: .03em;
  color: var(--muted);
  background: var(--badge-bg);
  border-radius: 4px;
  padding: .05rem .4rem;
}
.authors {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: .3rem;
  font-size: .88rem;
}
.person {
  font: inherit;
  font-size: .85rem;
  color: var(--link);
  background: var(--chip-bg);
  border: 1px solid transparent;
  border-radius: 999px;
  padding: .05rem .55rem;
  cursor: pointer;
}
.person:hover { border-color: var(--link); }
.publication .doi {
  margin-top: .4rem;
  font-size: .82rem;
  color: var(--muted);
  overflow-wrap: anywhere;
}

.empty {
  text-align: center;
  color: var(--muted);
  padding: 2rem 1rem;
}
footer.site {
  border-top: 1px solid var(--border);
  color: var(--muted);
  font-size: .9rem;
  padding: 1.5rem 0 3rem;
}
.to-top {
  position: fixed;
  right: calc(16px + env(safe-area-inset-right, 0px));
  bottom: calc(16px + env(safe-area-inset-bottom, 0px));
  z-index: 20;
  font: inherit;
  font-size: .9rem;
  color: var(--chip-on-text);
  background: var(--chip-on-bg);
  border: 0;
  border-radius: 999px;
  padding: .5rem .9rem;
  box-shadow: 0 2px 6px rgba(0, 0, 0, .2);
  cursor: pointer;
}

@media (max-width: 600px) {
  header.site h1 { font-size: 1.5rem; }
  .toolbar { flex-direction: column; align-items: stretch; }
  .toolbar input { flex: none; }
  .toolbar .status { margin-left: 0; }
  .chips { grid-template-columns: 1fr; }
}
"""


PAGE_JS = """
(function () {
  var state = { people: new Set(), q: "", year: "", type: "" };
  var cards = Array.prototype.slice.call(
    document.querySelectorAll("#by-year .publication")
  );
  var sections = document.querySelectorAll("#by-year .year-section");
  var chips = document.querySelectorAll(".chip[data-orcid]");
  var search = document.getElementById("search");
  var yearSel = document.getElementById("year-filter");
  var typeSel = document.getElementById("type-filter");
  var status = document.getElementById("result-count");
  var newest = document.getElementById("newest-section");
  var empty = document.getElementById("empty");
  var reset = document.getElementById("reset");
  var toolbar = document.getElementById("toolbar");

  function isActive() {
    return state.people.size > 0 || state.q || state.year || state.type;
  }

  function writeHash() {
    var params = new URLSearchParams();
    if (state.people.size) params.set("faculty", Array.from(state.people).join(","));
    if (state.year) params.set("year", state.year);
    if (state.type) params.set("type", state.type);
    if (state.q) params.set("q", state.q);
    var hash = params.toString();
    history.replaceState(
      null, "", hash ? "#" + hash : location.pathname + location.search
    );
  }

  function readHash() {
    var raw = location.hash.slice(1);
    if (raw.indexOf("=") === -1) return;
    var params = new URLSearchParams(raw);
    state.people = new Set(
      (params.get("faculty") || "").split(",").filter(Boolean)
    );
    state.year = params.get("year") || "";
    state.type = params.get("type") || "";
    state.q = (params.get("q") || "").toLowerCase();
    search.value = params.get("q") || "";
    yearSel.value = state.year;
    typeSel.value = state.type;
    // A shared link with faculty selected opens the panel so the selection shows.
    if (state.people.size) document.getElementById("faculty-panel").open = true;
  }

  function apply(updateHash) {
    var shown = 0;
    cards.forEach(function (card) {
      var people = card.dataset.people.split(" ");
      var ok =
        (!state.people.size ||
          people.some(function (p) { return state.people.has(p); })) &&
        (!state.year || card.dataset.year === state.year) &&
        (!state.type || card.dataset.type === state.type) &&
        (!state.q || card.dataset.text.indexOf(state.q) !== -1);
      card.hidden = !ok;
      if (ok) shown++;
    });

    sections.forEach(function (section) {
      var visible = !!section.querySelector(".publication:not([hidden])");
      section.hidden = !visible;
      var link = document.querySelector('.year-nav a[data-jump="' + section.id + '"]');
      if (link) link.hidden = !visible;
    });

    chips.forEach(function (chip) {
      chip.setAttribute(
        "aria-pressed", state.people.has(chip.dataset.orcid) ? "true" : "false"
      );
    });

    var active = isActive();
    newest.hidden = !!active;
    reset.hidden = !active;
    empty.hidden = shown > 0;
    status.textContent = "Showing " + shown + " of " + cards.length + " publications";
    if (updateHash) writeHash();
  }

  chips.forEach(function (chip) {
    chip.addEventListener("click", function () {
      var id = chip.dataset.orcid;
      if (state.people.has(id)) state.people.delete(id);
      else state.people.add(id);
      apply(true);
    });
  });

  document.addEventListener("click", function (event) {
    var person = event.target.closest(".person");
    if (person) {
      state.people = new Set([person.dataset.orcid]);
      apply(true);
      toolbar.scrollIntoView({ block: "start" });
      return;
    }
    var jump = event.target.closest("a[data-jump]");
    if (jump) {
      event.preventDefault();
      var target = document.getElementById(jump.dataset.jump);
      if (target) target.scrollIntoView({ block: "start" });
    }
  });

  search.addEventListener("input", function () {
    state.q = search.value.trim().toLowerCase();
    apply(true);
  });
  yearSel.addEventListener("change", function () {
    state.year = yearSel.value;
    apply(true);
  });
  typeSel.addEventListener("change", function () {
    state.type = typeSel.value;
    apply(true);
  });

  function clearAll() {
    state = { people: new Set(), q: "", year: "", type: "" };
    search.value = "";
    yearSel.value = "";
    typeSel.value = "";
    apply(true);
  }
  reset.addEventListener("click", clearAll);
  document.getElementById("clear-faculty").addEventListener("click", function () {
    state.people.clear();
    apply(true);
  });

  document.getElementById("to-top").addEventListener("click", function () {
    window.scrollTo({ top: 0 });
  });

  window.addEventListener("hashchange", function () {
    readHash();
    apply(false);
  });

  readHash();
  apply(false);
})();
"""


def main():
    people = load_roster()

    if not people:
        raise RuntimeError(
            "No faculty with valid ORCID iDs were found in data/faculty.csv."
        )

    names = {person["orcid"]: person for person in people}
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

            publications[key]["people"].add(person["orcid"])

    if successful_requests == 0:
        raise RuntimeError(
            "Could not retrieve works for any faculty member. "
            "Check the ORCID IDs and the API request errors above."
        )

    all_works = list(publications.values())
    all_works.sort(
        key=lambda work: (year_key(work["year"]), work["title"].casefold()),
        reverse=True,
    )

    counts = defaultdict(int)
    for work in all_works:
        for orcid in work["people"]:
            counts[orcid] += 1

    newest = "".join(
        publication_card(work, names) for work in all_works[:10]
    )

    years = defaultdict(list)
    for work in all_works:
        years[work["year"] or "Date unknown"].append(work)
    sorted_years = sorted(years, key=year_key, reverse=True)

    def section_id(year):
        return "year-" + (year if re.fullmatch(r"\d{4}", year) else "unknown")

    year_sections = "".join(
        f'<section class="year-section" id="{safe(section_id(year))}">'
        f'<h2>{safe(year)} <small>({len(years[year])})</small></h2>'
        + "".join(publication_card(work, names) for work in years[year])
        + "</section>"
        for year in sorted_years
    )

    year_nav = "".join(
        f'<a href="#{safe(section_id(year))}" '
        f'data-jump="{safe(section_id(year))}">{safe(year)}</a>'
        for year in sorted_years
    )

    year_options = "".join(
        f'<option value="{safe(work_year)}">{safe(year)}</option>'
        for year in sorted_years
        for work_year in [year if re.fullmatch(r"\d{4}", year) else "unknown"]
    )

    type_options = "".join(
        f'<option value="{safe(label)}">{safe(label)}</option>'
        for label in sorted({work["type"] for work in all_works})
    )

    chips = "".join(
        '<li class="chip-item">'
        f'<button type="button" class="chip" aria-pressed="false" '
        f'data-orcid="{safe(person["orcid"])}">'
        f'<span>{safe(person["display"])}</span>'
        f'<span class="count">{counts[person["orcid"]]}</span></button>'
        f'<a class="orcid" href="https://orcid.org/{safe(person["orcid"])}" '
        f'target="_blank" rel="noopener" '
        f'aria-label="ORCID profile for {safe(person["display"])}" '
        f'title="ORCID profile">iD</a>'
        "</li>"
        for person in sorted(people, key=lambda p: p["name"].casefold())
    )

    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    no_works = "<p class=\"empty\">No public works were found.</p>"

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BU PopSci Faculty Publications</title>
<style>{PAGE_CSS}</style>
</head>
<body>
<header class="site">
  <div class="wrap">
    <h1>BU PopSci Faculty Publications</h1>
    <p>Public works listed on each person's ORCID record.</p>
    <p class="stats">{len(people)} faculty · {len(all_works)} works</p>
    <p><small>Last updated: {safe(updated)}</small></p>
  </div>
</header>

<main class="wrap">
  <details class="panel" id="faculty-panel">
    <summary>Faculty ({len(people)})</summary>
    <p class="panel-help">Select one or more people to show only their
      publications. The number is each person's publication count.</p>
    <ul class="chips">{chips}</ul>
    <p class="panel-actions">
      <button type="button" class="btn-link" id="clear-faculty">Clear faculty selection</button>
    </p>
  </details>

  <div class="toolbar" id="toolbar">
    <label class="sr-only" for="search">Search publications</label>
    <input id="search" type="search"
           placeholder="Search titles, journals, or faculty">
    <label class="sr-only" for="year-filter">Year</label>
    <select id="year-filter">
      <option value="">All years</option>{year_options}
    </select>
    <label class="sr-only" for="type-filter">Type</label>
    <select id="type-filter">
      <option value="">All types</option>{type_options}
    </select>
    <button type="button" class="btn-link" id="reset" hidden>Reset filters</button>
    <span class="status" id="result-count" aria-live="polite"></span>
  </div>

  <section id="newest-section">
    <h2>Newest publications</h2>
    {newest or no_works}
  </section>

  <h2>Publications by year</h2>
  <nav class="year-nav" aria-label="Jump to year">{year_nav}</nav>
  <div id="by-year">
    {year_sections or no_works}
  </div>
  <p class="empty" id="empty" hidden>No publications match these filters.</p>
</main>

<footer class="site">
  <div class="wrap">
    Missing publications? Add them to your public ORCID record; this page
    refreshes every Monday.
  </div>
</footer>

<button type="button" class="to-top" id="to-top">↑ Top</button>

<script>{PAGE_JS}</script>
</body>
</html>
"""

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
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

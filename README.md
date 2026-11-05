# Job Application AI System

A personal, local Python tool that collects job postings from LinkedIn via Apify,
assesses their fit against your profile, tracks them in Google Sheets, and
eventually autofills supported job applications.

---

## Project phases

| Phase | Status | Description |
|---|---|---|
| **1 — Import & Track** | ✅ Complete | Fetch Apify dataset → normalize → deduplicate → Google Sheets |
| 2 — Fit Assessment | 🔜 Next | Résumé parsing, profile config, rule-based + AI scoring |
| 3 — Cover Letters | 🔜 Planned | Requirement detection, tailored generation, Drive storage |
| 4 — Autofill | 🔜 Planned | Browser automation for one supported platform |

---

## Quick start (Phase 1)

### Prerequisites

- Python 3.11+
- A Windows machine with PowerShell

### 1 — Clone and create a virtual environment

```powershell
git clone https://github.com/jeremiah-brea23/Job-Auto-Application.git
cd "Job-Auto-Application"
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2 — Configure credentials

```powershell
Copy-Item .env.example .env
```

Edit `.env` and fill in:
- `APIFY_TOKEN` and `APIFY_DATASET_ID` — see [docs/SETUP_APIFY.md](docs/SETUP_APIFY.md)
- `GOOGLE_SERVICE_ACCOUNT_JSON`, `GOOGLE_SPREADSHEET_ID` — see [docs/SETUP_GOOGLE.md](docs/SETUP_GOOGLE.md)

### 3 — Test with the built-in fixture (no credentials needed)

```powershell
# Dry-run: see what would be imported
python cli.py import --fixture --dry-run

# Actually write to your Google Sheet
python cli.py import --fixture
```

### 4 — Import from your real Apify dataset

```powershell
python cli.py import --dataset-id YOUR_DATASET_ID
```

### 5 — Trigger a new scraper run and import

```powershell
python cli.py import --task-id YOUR_TASK_ID
```

---

## CLI reference

```
python cli.py [--verbose] COMMAND [OPTIONS]

Commands:
  import    Fetch, normalize, deduplicate, and write jobs to Google Sheets.
  inspect   Print raw items from a source to inspect field structure.

import options:
  --fixture              Use the built-in sample fixture
  --fixture-path PATH    Use a custom fixture JSON file
  --dataset-id ID        Read an existing Apify dataset
  --task-id ID           Run a saved Apify Task and fetch results
  --dry-run              Show what would happen without writing anything

inspect options:
  --fixture              Inspect the built-in fixture
  --dataset-id ID        Inspect an Apify dataset
  --limit N              Number of items to print (default: 2)
```

---

## Project structure

```
├── cli.py                        Entry point
├── config/
│   └── settings.py               Typed config loaded from .env
├── src/job_app/
│   ├── apify_client.py           Apify REST API wrapper
│   ├── normalizer.py             Raw item → JobRecord
│   ├── deduplicator.py           Prevent duplicate sheet rows
│   ├── sheets_client.py          Google Sheets read/write
│   └── pipeline.py               Orchestrates the full import flow
├── fixtures/
│   └── sample_jobs.json          Realistic test data (5 LinkedIn jobs)
├── docs/
│   ├── SETUP_GOOGLE.md           Google Cloud & Sheets setup walkthrough
│   └── SETUP_APIFY.md            Apify token & dataset ID instructions
├── credentials/                  ← NOT in Git (service account JSON goes here)
├── .env                          ← NOT in Git (copy from .env.example)
├── .env.example                  Safe placeholder for all env vars
└── requirements.txt
```

---

## Google Sheets column layout

| Column | Field | Notes |
|---|---|---|
| A | source | `linkedin_apify` or `fixture` |
| B | job_id | LinkedIn numeric ID |
| C | title | Job title |
| D | company | Company name |
| E | location | City, state, or Remote |
| F | workplace_type | Remote / Hybrid / On-site |
| G | employment_type | Internship / Full-time / … |
| H | seniority_level | Internship / Entry / … |
| I | posting_url | LinkedIn job URL |
| J | apply_url | Direct application URL |
| K | posted_date | UTC ISO 8601 |
| L | discovered_date | When first imported |
| M | easy_apply | Yes / No |
| N | skills | Comma-separated |
| O | description_snippet | First 500 characters |
| P | fit_score | Phase 2 |
| Q | fit_reason | Phase 2 |
| R | missing_requirements | Phase 2 |
| S | application_status | Discovered / Needs Review / Ready / … |
| T | cover_letter_required | Phase 3 |
| U | cover_letter_link | Phase 3 |
| V | date_applied | Phase 4 |
| W | notes | Manual or error notes |

---

## Security notes

- `.env`, `credentials/`, `resumes/`, `cover_letters/`, and `profile/` are
  all in `.gitignore` and will never be committed.
- Scraped job descriptions are treated as untrusted data — their content
  cannot override program rules or access secrets.
- The Apify token is sent only in the `Authorization` header, never in URLs.

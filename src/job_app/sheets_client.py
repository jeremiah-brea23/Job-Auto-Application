"""
src/job_app/sheets_client.py
─────────────────────────────
Google Sheets read/write for job tracking.

One row = one job.  The sheet has a fixed header row (row 1).
All subsequent rows are job data.

Column layout (matches SHEET_COLUMNS order):
  A  source           B  job_id          C  title
  D  company          E  location        F  workplace_type
  G  employment_type  H  seniority_level I  posting_url
  J  apply_url        K  posted_date     L  discovered_date
  M  easy_apply       N  skills          O  description_snippet
  P  fit_score        Q  fit_reason      R  missing_requirements
  S  application_status  T  cover_letter_required  U  cover_letter_link
  V  date_applied     W  notes

Columns P–W are left empty on import; they are filled by later phases.

Authentication: Google service account JSON key.
See docs/SETUP_GOOGLE.md for how to create the key and share the sheet.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import gspread
from gspread.exceptions import APIError
from google.oauth2.service_account import Credentials

from src.job_app.normalizer import JobRecord

logger = logging.getLogger(__name__)

# ── Column definitions ────────────────────────────────────────
SHEET_COLUMNS = [
    "source",
    "job_id",
    "title",
    "company",
    "location",
    "workplace_type",
    "employment_type",
    "seniority_level",
    "posting_url",
    "apply_url",
    "posted_date",
    "discovered_date",
    "easy_apply",
    "skills",
    "description_snippet",
    # ── Phase 2+ columns (written empty now, filled later) ──
    "fit_score",
    "fit_reason",
    "missing_requirements",
    # ── Tracking columns ─────────────────────────────────────
    "application_status",
    "cover_letter_required",
    "cover_letter_link",
    "date_applied",
    "notes",
]

# Valid application statuses
VALID_STATUSES = {
    "Discovered", "Needs Review", "Ready", "In Progress",
    "Applied", "Skipped", "Error",
}

# Google API scopes needed
_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

# Max characters to store from description (keeps the sheet readable)
_DESCRIPTION_SNIPPET_LEN = 500

# Rate-limit: Sheets API allows ~60 write requests/minute per user
_WRITE_BACKOFF_SECONDS = 1.1


class SheetsClient:
    """Read/write jobs in a Google Sheet using a service-account credential."""

    def __init__(
        self,
        service_account_json: Path,
        spreadsheet_id: str,
        worksheet_name: str = "Jobs",
        dry_run: bool = False,
    ) -> None:
        self._spreadsheet_id = spreadsheet_id
        self._worksheet_name = worksheet_name
        self._dry_run = dry_run

        if not dry_run:
            self._gc, self._ws = self._connect(service_account_json)
        else:
            logger.info("[DRY RUN] Skipping Google Sheets connection.")
            self._gc = None  # type: ignore[assignment]
            self._ws = None  # type: ignore[assignment]

    # ─────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────

    def ensure_header(self) -> None:
        """
        Write the header row if the sheet is empty or the first row doesn't
        match. Does nothing in dry-run mode.
        """
        if self._dry_run:
            logger.info("[DRY RUN] Would write header row: %s", SHEET_COLUMNS)
            return

        first_row = self._ws.row_values(1)
        if first_row == SHEET_COLUMNS:
            logger.debug("Header row already correct.")
            return

        if first_row:
            logger.warning(
                "Row 1 exists but doesn't match expected columns. "
                "Found: %s — NOT overwriting to protect existing data. "
                "If this is a fresh sheet, clear row 1 manually and rerun.",
                first_row,
            )
            return

        logger.info("Writing header row …")
        self._ws.append_row(SHEET_COLUMNS, value_input_option="RAW")

    def read_all_rows(self) -> list[dict[str, str]]:
        """
        Return all data rows as a list of dicts keyed by column name.
        The header row is used as keys. Returns [] in dry-run mode.
        """
        if self._dry_run:
            logger.info("[DRY RUN] Would read existing rows.")
            return []

        all_values = self._ws.get_all_records(expected_headers=SHEET_COLUMNS)
        logger.info("Read %d existing rows from sheet.", len(all_values))
        return [
            {k: str(v) for k, v in row.items()}
            for row in all_values
        ]

    def append_jobs(self, records: list[JobRecord]) -> int:
        """
        Append new job rows to the sheet.

        Returns the number of rows actually written.
        Does nothing in dry-run mode (logs what would be written).
        """
        if not records:
            logger.info("No new records to append.")
            return 0

        rows = [self._record_to_row(r) for r in records]

        if self._dry_run:
            logger.info("[DRY RUN] Would append %d rows:", len(rows))
            for row in rows:
                logger.info("  → %s | %s | %s", row[2], row[3], row[0])
            return 0

        # Batch append in chunks of 50 to stay within rate limits
        chunk_size = 50
        written = 0
        for i in range(0, len(rows), chunk_size):
            chunk = rows[i : i + chunk_size]
            self._ws.append_rows(chunk, value_input_option="RAW")
            written += len(chunk)
            logger.info(
                "Appended rows %d–%d of %d …",
                i + 1, i + len(chunk), len(rows),
            )
            if i + chunk_size < len(rows):
                time.sleep(_WRITE_BACKOFF_SECONDS)

        logger.info("Total rows written to sheet: %d", written)
        return written

    def update_row_field(self, row_number: int, field_name: str, value: str) -> None:
        """
        Update a single cell in an existing row.
        row_number is 1-indexed (row 1 is the header, data starts at 2).
        """
        if field_name not in SHEET_COLUMNS:
            raise ValueError(f"Unknown column: {field_name!r}")

        col_index = SHEET_COLUMNS.index(field_name) + 1  # 1-indexed for Sheets

        if self._dry_run:
            logger.info(
                "[DRY RUN] Would update row %d, col %s (%s) → %r",
                row_number, field_name, _col_letter(col_index), value,
            )
            return

        self._ws.update_cell(row_number, col_index, value)

    def update_row_fields(self, row_number: int, fields: dict[str, str]) -> None:
        """
        Update multiple cells in a single row.
        Each field is written with backoff to handle Sheets rate limits.
        fields: {column_name: value}
        """
        if self._dry_run:
            for field_name, value in fields.items():
                col_index = SHEET_COLUMNS.index(field_name) + 1
                logger.info(
                    "[DRY RUN] Would update row %d, col %s (%s) → %r",
                    row_number, field_name, _col_letter(col_index), value,
                )
            return

        for field_name, value in fields.items():
            if field_name not in SHEET_COLUMNS:
                raise ValueError(f"Unknown column: {field_name!r}")
            col_index = SHEET_COLUMNS.index(field_name) + 1
            cell_addr = f"{_col_letter(col_index)}{row_number}"
            _write_with_backoff(self._ws, cell_addr, value)

    # ─────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────

    def _connect(
        self, service_account_json: Path
    ) -> tuple[gspread.Client, gspread.Worksheet]:
        if not service_account_json.exists():
            raise FileNotFoundError(
                f"Google service account JSON not found at: {service_account_json}\n"
                f"See docs/SETUP_GOOGLE.md for setup instructions."
            )

        creds = Credentials.from_service_account_file(
            str(service_account_json), scopes=_SCOPES
        )
        gc = gspread.authorize(creds)
        spreadsheet = gc.open_by_key(self._spreadsheet_id)

        # Get or create the target worksheet
        try:
            ws = spreadsheet.worksheet(self._worksheet_name)
            logger.info(
                "Connected to sheet '%s' (tab: %s)",
                self._spreadsheet_id, self._worksheet_name,
            )
        except gspread.WorksheetNotFound:
            logger.info(
                "Worksheet '%s' not found — creating it …", self._worksheet_name
            )
            ws = spreadsheet.add_worksheet(
                title=self._worksheet_name, rows=1000, cols=len(SHEET_COLUMNS)
            )

        return gc, ws

    def _record_to_row(self, r: JobRecord) -> list[str]:
        """Convert a JobRecord to an ordered list of strings matching SHEET_COLUMNS."""
        snippet = r.description[:_DESCRIPTION_SNIPPET_LEN].replace("\n", " ").strip()
        if len(r.description) > _DESCRIPTION_SNIPPET_LEN:
            snippet += "…"

        return [
            r.source,                          # source
            r.job_id,                          # job_id
            r.title,                           # title
            r.company,                         # company
            r.location,                        # location
            r.workplace_type,                  # workplace_type
            r.employment_type,                 # employment_type
            r.seniority_level,                 # seniority_level
            r.posting_url,                     # posting_url
            r.apply_url,                       # apply_url
            r.posted_date,                     # posted_date
            r.discovered_date,                 # discovered_date
            "Yes" if r.easy_apply else "No",   # easy_apply
            ", ".join(r.skills),               # skills
            snippet,                           # description_snippet
            "",  # fit_score        (Phase 2)
            "",  # fit_reason       (Phase 2)
            "",  # missing_requirements (Phase 2)
            "Discovered",                      # application_status
            "",  # cover_letter_required (Phase 3)
            "",  # cover_letter_link     (Phase 3)
            "",  # date_applied
            "",  # notes
        ]


def _col_letter(n: int) -> str:
    """Convert a 1-indexed column number to a spreadsheet letter (1→A, 27→AA)."""
    result = ""
    while n:
        n, remainder = divmod(n - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _write_with_backoff(
    ws: gspread.Worksheet,
    cell_addr: str,
    value: str,
    max_retries: int = 5,
) -> None:
    """
    Write a single cell with exponential backoff on 429 rate-limit errors.
    Waits 15s, 30s, 60s, 120s before giving up.
    """
    delay = 15
    for attempt in range(max_retries):
        try:
            ws.update(cell_addr, [[value]], value_input_option="RAW")
            return
        except APIError as exc:
            if "429" in str(exc) and attempt < max_retries - 1:
                logger.warning(
                    "Rate limited writing %s — waiting %ds before retry %d/%d …",
                    cell_addr, delay, attempt + 1, max_retries - 1,
                )
                time.sleep(delay)
                delay = min(delay * 2, 120)
            else:
                raise

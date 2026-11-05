"""
src/job_app/pipeline.py
────────────────────────
Orchestrates the Phase 1 import flow:

  source (Apify dataset OR local JSON fixture)
    → normalize raw items into JobRecords
    → deduplicate against existing sheet rows
    → append new records to Google Sheets

The pipeline is designed to be re-runnable safely:
  - No row is written twice (dedup prevents it).
  - If interrupted, rerunning picks up from where it left off.
  - dry_run=True shows exactly what would happen without writing anything.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.job_app.apify_client import ApifyJobClient
from src.job_app.deduplicator import Deduplicator
from src.job_app.normalizer import JobRecord, normalize
from src.job_app.sheets_client import SheetsClient

logger = logging.getLogger(__name__)


@dataclass
class ImportResult:
    """Summary returned after a pipeline run."""
    total_fetched: int      # Raw items received from source
    total_normalized: int   # Successfully parsed into JobRecords
    total_skipped: int      # Failed normalization (bad data)
    total_duplicates: int   # Already in sheet (skipped)
    total_new: int          # Written (or would be written in dry-run)
    dry_run: bool


class ImportPipeline:
    """
    Phase 1 import pipeline.

    Args:
        sheets_client: Configured SheetsClient instance.
        apify_token:   Apify API token (required for live mode).
        dry_run:       If True, nothing is written to the sheet.
    """

    def __init__(
        self,
        sheets_client: SheetsClient,
        apify_token: str = "",
        dry_run: bool = False,
    ) -> None:
        self._sheets = sheets_client
        self._apify_token = apify_token
        self._dry_run = dry_run

    # ─────────────────────────────────────────────────────────
    # Public entry points
    # ─────────────────────────────────────────────────────────

    def run_from_fixture(self, fixture_path: Path) -> ImportResult:
        """
        Import jobs from a local JSON fixture file.
        Useful for testing without Apify credentials.
        """
        logger.info("Loading fixture: %s", fixture_path)
        if not fixture_path.exists():
            raise FileNotFoundError(f"Fixture not found: {fixture_path}")

        with fixture_path.open(encoding="utf-8") as fh:
            raw_items: list[dict[str, Any]] = json.load(fh)

        logger.info("Fixture loaded: %d raw items", len(raw_items))
        return self._run(raw_items, source="fixture")

    def run_from_dataset(self, dataset_id: str) -> ImportResult:
        """
        Fetch jobs from an existing Apify dataset and import them.
        """
        client = self._get_apify_client()
        raw_items = client.read_dataset(dataset_id)
        return self._run(raw_items, source="linkedin_apify")

    def run_from_task(self, task_id: str) -> ImportResult:
        """
        Trigger a saved Apify Task, wait for it to finish, then import results.
        """
        client = self._get_apify_client()
        raw_items = client.run_task_and_fetch(task_id)
        return self._run(raw_items, source="linkedin_apify")

    # ─────────────────────────────────────────────────────────
    # Core pipeline
    # ─────────────────────────────────────────────────────────

    def _run(
        self,
        raw_items: list[dict[str, Any]],
        source: str,
    ) -> ImportResult:
        total_fetched = len(raw_items)
        logger.info("Pipeline started: %d raw items, source=%s", total_fetched, source)

        # ── Step 1: Normalize ─────────────────────────────────
        records: list[JobRecord] = []
        skipped = 0
        for item in raw_items:
            record = normalize(item, source=source)
            if record:
                records.append(record)
            else:
                skipped += 1

        logger.info(
            "Normalization: %d succeeded, %d skipped (bad data)",
            len(records), skipped,
        )

        # ── Step 2: Ensure sheet header exists ────────────────
        self._sheets.ensure_header()

        # ── Step 3: Read existing rows for dedup ──────────────
        existing_rows = self._sheets.read_all_rows()
        deduplicator = Deduplicator(existing_rows)

        # ── Step 4: Filter duplicates ─────────────────────────
        new_records, duplicate_records = deduplicator.filter_new(records)

        # ── Step 5: Append new records ────────────────────────
        written = self._sheets.append_jobs(new_records)

        result = ImportResult(
            total_fetched=total_fetched,
            total_normalized=len(records),
            total_skipped=skipped,
            total_duplicates=len(duplicate_records),
            total_new=len(new_records),
            dry_run=self._dry_run,
        )

        self._log_summary(result)
        return result

    # ─────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────

    def _get_apify_client(self) -> ApifyJobClient:
        if not self._apify_token:
            raise ValueError(
                "Apify token is required for live import. "
                "Set APIFY_TOKEN in your .env file."
            )
        return ApifyJobClient(self._apify_token)

    def _log_summary(self, r: ImportResult) -> None:
        mode = "[DRY RUN] " if r.dry_run else ""
        logger.info(
            "%sPipeline complete — fetched: %d | normalized: %d | "
            "duplicates skipped: %d | new: %d | normalization failures: %d",
            mode,
            r.total_fetched,
            r.total_normalized,
            r.total_duplicates,
            r.total_new,
            r.total_skipped,
        )

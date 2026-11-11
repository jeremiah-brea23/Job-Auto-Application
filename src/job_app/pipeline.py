"""
src/job_app/pipeline.py
────────────────────────
Orchestrates the full import + assessment flow:

  Phase 1 — Import
    source (Apify dataset OR local JSON fixture)
      → normalize raw items into JobRecords
      → deduplicate against existing sheet rows
      → append new records to Google Sheets

  Phase 2 — Assess
    Read unassessed rows from the sheet
      → run rule-based + optional AI fit assessment
      → write fit_score, fit_reason, missing_requirements, application_status back

Both phases are re-runnable safely:
  - No row is written twice (dedup prevents it).
  - Assess only targets rows where fit_score is still empty.
  - dry_run=True shows what would happen without writing anything.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.job_app.apify_client import ApifyJobClient
from src.job_app.deduplicator import Deduplicator
from src.job_app.normalizer import JobRecord, normalize
from src.job_app.sheets_client import SheetsClient, SHEET_COLUMNS

logger = logging.getLogger(__name__)


@dataclass
class ImportResult:
    """Summary returned after a pipeline run."""
    total_fetched: int
    total_normalized: int
    total_skipped: int
    total_duplicates: int
    total_new: int
    dry_run: bool


@dataclass
class AssessResult:
    """Summary returned after an assessment run."""
    total_rows: int
    already_assessed: int
    assessed_now: int
    score_breakdown: dict[str, int]
    dry_run: bool


class ImportPipeline:
    """
    Import + Assessment pipeline.

    Args:
        sheets_client: Configured SheetsClient instance.
        apify_token:   Apify API token (required for live import).
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
    # Phase 1: Import entry points
    # ─────────────────────────────────────────────────────────

    def run_from_fixture(self, fixture_path: Path) -> ImportResult:
        """Import jobs from a local JSON fixture file."""
        logger.info("Loading fixture: %s", fixture_path)
        if not fixture_path.exists():
            raise FileNotFoundError(f"Fixture not found: {fixture_path}")

        with fixture_path.open(encoding="utf-8") as fh:
            raw_items: list[dict[str, Any]] = json.load(fh)

        logger.info("Fixture loaded: %d raw items", len(raw_items))
        return self._run_import(raw_items, source="fixture")

    def run_from_dataset(self, dataset_id: str) -> ImportResult:
        """Fetch jobs from an existing Apify dataset and import them."""
        client = self._get_apify_client()
        raw_items = client.read_dataset(dataset_id)
        return self._run_import(raw_items, source="linkedin_apify")

    def run_from_task(self, task_id: str) -> ImportResult:
        """Trigger a saved Apify Task, wait for it to finish, import results."""
        client = self._get_apify_client()
        raw_items = client.run_task_and_fetch(task_id)
        return self._run_import(raw_items, source="linkedin_apify")

    # ─────────────────────────────────────────────────────────
    # Phase 2: Assessment entry point
    # ─────────────────────────────────────────────────────────

    def run_assessment(
        self,
        profile_path: Path,
        resume_path: Path | None,
        ai_api_key: str = "",
        ai_model: str = "gpt-4o-mini",
        limit: int | None = None,
    ) -> AssessResult:
        """
        Assess all unassessed jobs in the sheet.

        Args:
            profile_path: Path to profile/profile.yaml.
            resume_path:  Path to the résumé file (optional — skips AI context if missing).
            ai_api_key:   OpenAI API key (empty = rules only).
            ai_model:     Model name.
            limit:        Max rows to assess in this run (None = all).
        """
        from src.job_app.profile_loader import load_profile
        from src.job_app.resume_parser import parse_resume
        from src.job_app.assessor import JobAssessor

        # Load profile
        profile = load_profile(profile_path)

        # Load résumé text (best-effort)
        resume_text = ""
        if resume_path and resume_path.exists():
            try:
                resume_text = parse_resume(resume_path)
                logger.info("Résumé loaded: %d characters", len(resume_text))
            except Exception as exc:
                logger.warning("Could not parse résumé: %s — proceeding without it", exc)
        else:
            logger.info("No résumé file found — AI assessment will run without it.")

        assessor = JobAssessor(
            profile=profile,
            resume_text=resume_text,
            ai_api_key=ai_api_key,
            ai_model=ai_model,
        )

        # Read all rows from the sheet
        self._sheets.ensure_header()
        all_rows = self._sheets.read_all_rows()
        total_rows = len(all_rows)

        # Find rows that haven't been assessed yet (fit_score is empty)
        unassessed: list[tuple[int, dict[str, str]]] = []  # (sheet_row_number, row_dict)
        for i, row in enumerate(all_rows):
            if not row.get("fit_score", "").strip():
                sheet_row = i + 2  # +1 for 0-index, +1 for header row
                unassessed.append((sheet_row, row))

        already_assessed = total_rows - len(unassessed)
        logger.info(
            "Rows in sheet: %d | already assessed: %d | to assess: %d",
            total_rows, already_assessed, len(unassessed),
        )

        if limit:
            unassessed = unassessed[:limit]
            logger.info("Limiting to %d rows for this run", limit)

        # Reconstruct JobRecords from sheet rows for assessment
        records_to_assess: list[tuple[int, JobRecord]] = []
        for sheet_row, row in unassessed:
            jr = _row_to_job_record(row)
            if jr:
                records_to_assess.append((sheet_row, jr))

        # Run assessments
        score_breakdown: dict[str, int] = {
            "Good": 0, "Maybe": 0, "Skip": 0, "Needs Review": 0
        }
        assessed_now = 0

        for sheet_row, record in records_to_assess:
            result = assessor.assess(record)
            score_breakdown[result.fit_score] = score_breakdown.get(result.fit_score, 0) + 1

            if self._dry_run:
                logger.info(
                    "[DRY RUN] Row %d: %s @ %s → %s | %s",
                    sheet_row, record.title, record.company,
                    result.fit_score, result.fit_reason[:60],
                )
            else:
                # Batch all 4 assessment fields into a single API call per row
                self._sheets.update_row_fields(sheet_row, {
                    "fit_score": result.fit_score,
                    "fit_reason": result.fit_reason,
                    "missing_requirements": result.missing_requirements,
                    "application_status": result.suggested_status,
                })
                logger.info(
                    "Assessed row %d: %s @ %s → %s",
                    sheet_row, record.title, record.company, result.fit_score,
                )
                # Respect Sheets API rate limit: ~60 write requests/minute
                time.sleep(2.0)
            assessed_now += 1

        result_summary = AssessResult(
            total_rows=total_rows,
            already_assessed=already_assessed,
            assessed_now=assessed_now,
            score_breakdown=score_breakdown,
            dry_run=self._dry_run,
        )
        self._log_assess_summary(result_summary)
        return result_summary

    # ─────────────────────────────────────────────────────────
    # Core import pipeline
    # ─────────────────────────────────────────────────────────

    def _run_import(
        self,
        raw_items: list[dict[str, Any]],
        source: str,
    ) -> ImportResult:
        total_fetched = len(raw_items)
        logger.info("Pipeline started: %d raw items, source=%s", total_fetched, source)

        records: list[JobRecord] = []
        skipped = 0
        for item in raw_items:
            record = normalize(item, source=source)
            if record:
                records.append(record)
            else:
                skipped += 1

        logger.info("Normalization: %d succeeded, %d skipped", len(records), skipped)

        self._sheets.ensure_header()
        existing_rows = self._sheets.read_all_rows()
        deduplicator = Deduplicator(existing_rows)
        new_records, duplicate_records = deduplicator.filter_new(records)
        self._sheets.append_jobs(new_records)

        result = ImportResult(
            total_fetched=total_fetched,
            total_normalized=len(records),
            total_skipped=skipped,
            total_duplicates=len(duplicate_records),
            total_new=len(new_records),
            dry_run=self._dry_run,
        )
        self._log_import_summary(result)
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

    def _log_import_summary(self, r: ImportResult) -> None:
        mode = "[DRY RUN] " if r.dry_run else ""
        logger.info(
            "%sPipeline complete — fetched: %d | normalized: %d | "
            "duplicates skipped: %d | new: %d | normalization failures: %d",
            mode, r.total_fetched, r.total_normalized,
            r.total_duplicates, r.total_new, r.total_skipped,
        )

    def _log_assess_summary(self, r: AssessResult) -> None:
        mode = "[DRY RUN] " if r.dry_run else ""
        logger.info(
            "%sAssessment complete — assessed: %d | Good: %d | Maybe: %d | "
            "Skip: %d | Needs Review: %d",
            mode, r.assessed_now,
            r.score_breakdown.get("Good", 0),
            r.score_breakdown.get("Maybe", 0),
            r.score_breakdown.get("Skip", 0),
            r.score_breakdown.get("Needs Review", 0),
        )


def _row_to_job_record(row: dict[str, str]) -> JobRecord | None:
    """Reconstruct a minimal JobRecord from a sheet row dict for re-assessment."""
    try:
        from src.job_app.normalizer import JobRecord
        return JobRecord(
            job_id=row.get("job_id", ""),
            source=row.get("source", ""),
            title=row.get("title", "Unknown Title"),
            company=row.get("company", ""),
            location=row.get("location", ""),
            description=row.get("description_snippet", ""),
            posting_url=row.get("posting_url", ""),
            apply_url=row.get("apply_url", ""),
            posted_date=row.get("posted_date", ""),
            discovered_date=row.get("discovered_date", ""),
            workplace_type=row.get("workplace_type", ""),
            employment_type=row.get("employment_type", ""),
            seniority_level=row.get("seniority_level", ""),
            easy_apply=row.get("easy_apply", "").lower() == "yes",
            skills=[s.strip() for s in row.get("skills", "").split(",") if s.strip()],
        )
    except Exception as exc:
        logger.warning("Could not reconstruct JobRecord from row: %s", exc)
        return None

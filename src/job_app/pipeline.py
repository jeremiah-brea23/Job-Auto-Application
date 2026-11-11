"""
src/job_app/pipeline.py
────────────────────────
Orchestrates the full import + assessment + cover letter flow:

  Phase 1 — Import
    source → normalize → deduplicate → append to Google Sheets

  Phase 2 — Assess
    Unscored rows → rule-based + optional AI → write scores back

  Phase 3 — Cover Letters
    Ready/Maybe rows → detect requirement from description
                     → generate tailored letter (AI)
                     → save locally + upload to Google Drive
                     → write cover_letter_required + link to sheet

All phases are re-runnable safely (idempotent).
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
class CoverLetterRunResult:
    """Summary returned after a cover letter generation run."""
    total_eligible: int        # Ready/Maybe rows with no letter yet
    already_have_letter: int   # Rows that already had a link
    detection_only: int        # Rows where requirement was detected but no letter generated
    generated: int             # Letters successfully generated
    uploaded: int              # Letters uploaded to Drive
    flagged_for_review: int    # Unknown requirement → flagged
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
    # Phase 3: Cover letter generation
    # ─────────────────────────────────────────────────────────

    def run_cover_letters(
        self,
        profile_path: Path,
        resume_path: Path,
        base_letter_path: Path,
        output_dir: Path,
        ai_api_key: str,
        ai_model: str,
        candidate_name: str = "",
        service_account_json: Path | None = None,
        drive_folder_id: str = "",
        produce_docx: bool = False,
        statuses_to_process: list[str] | None = None,
        limit: int | None = None,
    ) -> CoverLetterRunResult:
        """
        For every eligible row in the sheet:
          1. Detect cover letter requirement from the description snippet.
          2. Write the detection result to cover_letter_required if empty.
          3. If required (or optional and generate_for_optional=True):
             - Generate a tailored cover letter via AI.
             - Save locally.
             - Upload to Google Drive if credentials are available.
             - Write the Drive link to cover_letter_link in the sheet.
          4. If unknown: flag the row (set cover_letter_required = "unknown").

        Eligible rows: application_status in statuses_to_process and
                       cover_letter_link is empty.

        Args:
            statuses_to_process: Statuses to include. Default: ["Ready", "Maybe"].
        """
        from src.job_app.profile_loader import load_profile
        from src.job_app.resume_parser import parse_resume
        from src.job_app.cover_letter_detector import detect_cover_letter_requirement
        from src.job_app.cover_letter_generator import CoverLetterGenerator

        if statuses_to_process is None:
            statuses_to_process = ["Ready", "Maybe"]

        profile = load_profile(profile_path)

        # Parse résumé
        resume_text = ""
        if resume_path and resume_path.exists():
            try:
                resume_text = parse_resume(resume_path)
                logger.info("Résumé loaded: %d characters", len(resume_text))
            except Exception as exc:
                logger.warning("Could not parse résumé: %s", exc)

        # Build generator (requires AI key)
        generator: CoverLetterGenerator | None = None
        if ai_api_key:
            try:
                generator = CoverLetterGenerator(
                    profile=profile,
                    resume_text=resume_text,
                    base_letter_path=base_letter_path,
                    output_dir=output_dir,
                    ai_api_key=ai_api_key,
                    ai_model=ai_model,
                    candidate_name=candidate_name,
                )
            except Exception as exc:
                logger.warning("Could not init cover letter generator: %s", exc)

        # Build Drive client (optional)
        from src.job_app.drive_client import DriveClient
        drive: DriveClient | None = None
        if service_account_json and service_account_json.exists():
            try:
                drive = DriveClient(service_account_json, folder_id=drive_folder_id)
            except Exception as exc:
                logger.warning("Could not init Drive client: %s — letters won't be uploaded", exc)

        # Read sheet rows
        self._sheets.ensure_header()
        all_rows = self._sheets.read_all_rows()

        eligible: list[tuple[int, dict[str, str]]] = []
        already_have_letter = 0
        for i, row in enumerate(all_rows):
            status = row.get("application_status", "").strip()
            if status not in statuses_to_process:
                continue
            if row.get("cover_letter_link", "").strip():
                already_have_letter += 1
                continue
            sheet_row = i + 2
            eligible.append((sheet_row, row))

        logger.info(
            "Cover letter run: %d eligible rows, %d already have a link",
            len(eligible), already_have_letter,
        )

        if limit:
            eligible = eligible[:limit]

        generated = 0
        uploaded = 0
        flagged = 0
        detection_only = 0

        for sheet_row, row in eligible:
            # Reconstruct JobRecord from row
            record = _row_to_job_record(row)
            if not record:
                continue

            # ── Step 1: Detect requirement ────────────────────
            current_cl_req = row.get("cover_letter_required", "").strip()
            if not current_cl_req:
                requirement = detect_cover_letter_requirement(record.description)
            else:
                requirement = current_cl_req

            if self._dry_run:
                logger.info(
                    "[DRY RUN] Row %d: %s @ %s — cover letter: %s",
                    sheet_row, record.title, record.company, requirement,
                )
                if requirement == "unknown":
                    flagged += 1
                else:
                    detection_only += 1
                continue

            # Write detection result if not already set
            if not current_cl_req:
                self._sheets.update_row_fields(sheet_row, {
                    "cover_letter_required": requirement,
                })
                time.sleep(2.0)

            # ── Step 2: Handle unknown → flag for review ──────
            if requirement == "unknown":
                # Update status to Needs Review so it surfaces for manual check
                self._sheets.update_row_fields(sheet_row, {
                    "notes": "Cover letter requirement unknown — verify application form",
                })
                time.sleep(2.0)
                flagged += 1
                logger.info(
                    "Row %d flagged for review (unknown CL requirement): %s @ %s",
                    sheet_row, record.title, record.company,
                )
                continue

            # ── Step 3: Generate letter if requirement is known ─
            if generator is None:
                logger.warning(
                    "AI key not set — cannot generate letter for row %d. "
                    "Set AI_API_KEY in .env.", sheet_row,
                )
                detection_only += 1
                continue

            try:
                cl_result = generator.generate(record, produce_docx=produce_docx)
                generated += 1
                logger.info("Generated letter for row %d: %s", sheet_row, record.title)
            except Exception as exc:
                logger.error("Failed to generate letter for row %d: %s", sheet_row, exc)
                self._sheets.update_row_fields(sheet_row, {
                    "notes": f"Cover letter generation failed: {exc}",
                })
                time.sleep(2.0)
                continue

            # ── Step 4: Upload to Drive ────────────────────────
            drive_link = ""
            if drive:
                try:
                    # Upload as Google Doc (editable, shareable)
                    drive_link = drive.upload_txt_as_doc(cl_result.txt_path)
                    uploaded += 1
                except Exception as exc:
                    logger.warning(
                        "Drive upload failed for row %d: %s — link will be local path",
                        sheet_row, exc,
                    )
                    drive_link = str(cl_result.txt_path)
            else:
                # Store local path as fallback
                drive_link = str(cl_result.txt_path)

            # ── Step 5: Write link back to sheet ──────────────
            self._sheets.update_row_fields(sheet_row, {
                "cover_letter_link": drive_link,
            })
            time.sleep(2.0)
            logger.info("Row %d: cover letter link written → %s", sheet_row, drive_link[:60])

        summary = CoverLetterRunResult(
            total_eligible=len(eligible),
            already_have_letter=already_have_letter,
            detection_only=detection_only,
            generated=generated,
            uploaded=uploaded,
            flagged_for_review=flagged,
            dry_run=self._dry_run,
        )
        self._log_cl_summary(summary)
        return summary

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

    def _log_cl_summary(self, r: CoverLetterRunResult) -> None:
        mode = "[DRY RUN] " if r.dry_run else ""
        logger.info(
            "%sCover letters — eligible: %d | already had link: %d | "
            "generated: %d | uploaded: %d | detection only: %d | flagged: %d",
            mode, r.total_eligible, r.already_have_letter,
            r.generated, r.uploaded, r.detection_only, r.flagged_for_review,
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

"""
src/job_app/deduplicator.py
────────────────────────────
Determines which incoming JobRecords are new versus already tracked in
Google Sheets, so reruns are safe and idempotent.

Strategy (in priority order):
  1. source + job_id  →  the strongest signal; same job from the same source
  2. normalised posting URL  →  fallback when IDs differ but URL matches

The deduplicator is stateless between runs: it reads existing dedup keys
directly from the Sheets data passed in, so no separate local database is
needed for Phase 1.
"""

from __future__ import annotations

import logging
from typing import Sequence

from src.job_app.normalizer import JobRecord

logger = logging.getLogger(__name__)


class Deduplicator:
    """
    Builds an in-memory index of already-tracked jobs from existing sheet rows,
    then answers "is this JobRecord new?" for each incoming record.
    """

    def __init__(self, existing_rows: list[dict[str, str]]) -> None:
        """
        Args:
            existing_rows: List of row dicts already in Google Sheets.
                           Expected keys: 'source', 'job_id', 'posting_url'.
                           Missing keys are tolerated.
        """
        self._seen_keys: set[str] = set()
        self._seen_urls: set[str] = set()
        self._build_index(existing_rows)
        logger.debug(
            "Deduplicator built: %d dedup keys, %d URL fallbacks",
            len(self._seen_keys),
            len(self._seen_urls),
        )

    # ─────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────

    def is_duplicate(self, record: JobRecord) -> bool:
        """Return True if this record is already in the sheet."""
        # Primary check: source::job_id key
        if record.job_id and record.dedup_key in self._seen_keys:
            logger.debug("Duplicate (by key): %s — %s", record.dedup_key, record.title)
            return True

        # Fallback: normalised posting URL
        if record.posting_url and record.posting_url in self._seen_urls:
            logger.debug("Duplicate (by URL): %s — %s", record.posting_url, record.title)
            return True

        return False

    def filter_new(self, records: Sequence[JobRecord]) -> tuple[list[JobRecord], list[JobRecord]]:
        """
        Split records into (new_records, duplicate_records).
        Also registers new records so repeated calls within the same run
        don't produce internal duplicates.
        """
        new: list[JobRecord] = []
        duplicates: list[JobRecord] = []

        for record in records:
            if self.is_duplicate(record):
                duplicates.append(record)
            else:
                new.append(record)
                self._register(record)  # prevent intra-batch duplicates

        logger.info(
            "Dedup result: %d new, %d already tracked (skipped)",
            len(new),
            len(duplicates),
        )
        return new, duplicates

    # ─────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────

    def _build_index(self, rows: list[dict[str, str]]) -> None:
        for row in rows:
            source = row.get("source", "").strip()
            job_id = row.get("job_id", "").strip()
            url = row.get("posting_url", "").strip()

            if source and job_id:
                self._seen_keys.add(f"{source}::{job_id}")
            if url:
                self._seen_urls.add(url)

    def _register(self, record: JobRecord) -> None:
        """Add a newly accepted record to the index to prevent intra-batch dupes."""
        if record.job_id:
            self._seen_keys.add(record.dedup_key)
        if record.posting_url:
            self._seen_urls.add(record.posting_url)

"""
src/job_app/apify_client.py
────────────────────────────
Thin wrapper around the Apify platform for two operations:

  1. read_dataset(dataset_id)
       Fetches all items from an existing dataset (from your successful run).

  2. run_task_and_fetch(task_id)
       Triggers a saved Apify Task, waits for it to finish, then fetches
       the resulting dataset items.

Both return a list of raw dicts — the normalizer turns them into JobRecords.

Authentication: Bearer token in the Authorization header (recommended by
Apify docs over passing token as a URL parameter).

Reference: https://docs.apify.com/api/v2
"""

from __future__ import annotations

import logging
import time
from typing import Any

from apify_client import ApifyClient as _ApifyClient

logger = logging.getLogger(__name__)

# How long to poll while waiting for a Task run to complete
_POLL_INTERVAL_SECONDS = 10
_MAX_WAIT_SECONDS = 600  # 10 minutes


class ApifyJobClient:
    """Apify API client scoped to job-data operations."""

    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("Apify token is required. Set APIFY_TOKEN in your .env file.")
        self._client = _ApifyClient(token)
        logger.debug("ApifyJobClient initialised (token length=%d)", len(token))

    # ─────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────

    def read_dataset(self, dataset_id: str) -> list[dict[str, Any]]:
        """
        Fetch all items from an existing Apify dataset.

        Args:
            dataset_id: The dataset ID shown in Apify Console → Storage → Datasets,
                        or the defaultDatasetId from a run response.

        Returns:
            List of raw item dicts as returned by Apify.
        """
        if not dataset_id:
            raise ValueError(
                "dataset_id is required. Set APIFY_DATASET_ID in your .env file, "
                "or pass --dataset-id on the CLI."
            )

        logger.info("Fetching dataset items (id=%s) …", dataset_id)
        dataset = self._client.dataset(dataset_id)

        items: list[dict[str, Any]] = []
        # iterate_items returns a generator; collect into a list
        for item in dataset.iterate_items():
            items.append(item)

        logger.info("Fetched %d items from dataset %s", len(items), dataset_id)
        return items

    def run_task_and_fetch(self, task_id: str) -> list[dict[str, Any]]:
        """
        Start a saved Apify Task, wait for it to finish, and return its items.

        Args:
            task_id: The task ID or ~username/task-name slug from Apify Console.

        Returns:
            List of raw item dicts from the task's default dataset.
        """
        if not task_id:
            raise ValueError(
                "task_id is required. Set APIFY_TASK_ID in your .env file, "
                "or pass --task-id on the CLI."
            )

        logger.info("Starting Apify Task (id=%s) …", task_id)
        task_client = self._client.task(task_id)
        run = task_client.call()  # call() starts + waits for completion

        run_id = run.get("id", "unknown")
        status = run.get("status", "unknown")
        logger.info("Task run completed (run_id=%s, status=%s)", run_id, status)

        if status != "SUCCEEDED":
            raise RuntimeError(
                f"Apify Task run {run_id} finished with status '{status}', not SUCCEEDED. "
                f"Check the run log at: https://console.apify.com/actors/runs/{run_id}"
            )

        dataset_id: str = run.get("defaultDatasetId", "")
        if not dataset_id:
            raise RuntimeError(
                f"Task run {run_id} succeeded but returned no defaultDatasetId."
            )

        logger.info("Task run dataset id: %s", dataset_id)
        return self.read_dataset(dataset_id)

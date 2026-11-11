"""
src/job_app/normalizer.py
─────────────────────────
Maps a raw Apify LinkedIn scraper item (dict) into a clean JobRecord dataclass.

The Apify LinkedIn scrapers differ slightly in field names across actors.
This module inspects the actual keys present and tries multiple aliases,
so one normalizer works for the most common variants without brittle key
assumptions.

Usage:
    from src.job_app.normalizer import normalize, JobRecord
    records = [normalize(item) for item in raw_items]
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)

SOURCE = "linkedin_apify"


# ─────────────────────────────────────────────────────────────────────────────
# JobRecord – the canonical internal representation of one job posting
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class JobRecord:
    # Identity
    job_id: str          # Source-specific ID (e.g. LinkedIn numeric ID)
    source: str          # "linkedin_apify" | "fixture" | …

    # Core fields
    title: str
    company: str
    location: str
    description: str

    # URLs
    posting_url: str     # Canonical URL to the job posting page
    apply_url: str       # Direct application URL (may be empty)

    # Dates (ISO 8601 strings, UTC)
    posted_date: str     # When the job was originally posted
    discovered_date: str # When we first saw it (set to now on import)

    # Extra metadata (nice-to-have, not required for dedup)
    workplace_type: str  # Remote | Hybrid | On-site | ""
    employment_type: str # Internship | Full-time | Part-time | Contract | ""
    seniority_level: str
    easy_apply: bool
    skills: list[str] = field(default_factory=list)

    # Dedup key – computed, not stored directly in Sheets
    dedup_key: str = field(init=False)

    def __post_init__(self) -> None:
        self.dedup_key = _make_dedup_key(self.source, self.job_id, self.posting_url)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def normalize(raw: dict[str, Any], source: str = SOURCE) -> JobRecord | None:
    """
    Convert a raw Apify item dict into a JobRecord.

    Returns None if the item is so incomplete that it cannot be usefully
    tracked (e.g. no title and no job ID). Logs a warning in that case.
    """
    try:
        return _build_record(raw, source)
    except Exception as exc:
        logger.warning("Failed to normalize item (skipping): %s | raw keys: %s",
                       exc, list(raw.keys()))
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _build_record(raw: dict[str, Any], source: str) -> JobRecord:
    job_id = _get_job_id(raw)
    title = _str(raw, "title", "jobTitle", "job_title", "name") or "Unknown Title"
    company = _get_company_name(raw)
    location = _str(raw, "location", "formattedLocation", "jobLocation", "city") or ""
    description = _str(raw, "descriptionText", "description", "jobDescription",
                        "descriptionHtml") or ""
    posting_url = _get_posting_url(raw, job_id)
    apply_url = _str(raw, "applyUrl", "apply_url", "externalApplyLink",
                     "applicationUrl", "applyLink") or ""
    posted_date = _parse_date(
        _str(raw, "postedAt", "posted_at", "listedAt", "listed_at",
             "publishedAt", "datePosted")
    )
    workplace_type = _get_workplace_type(raw)
    employment_type = _str(raw, "formattedEmploymentStatus", "employmentStatus",
                           "employment_type", "employmentType", "jobType") or ""
    seniority_level = _str(raw, "formattedExperienceLevel", "seniority_level",
                           "seniorityLevel", "experienceLevel") or ""
    easy_apply = bool(raw.get("easyApply") or raw.get("easy_apply") or False)
    skills = _get_skills(raw)

    if not job_id and not title:
        raise ValueError("Item has neither a job ID nor a title — cannot track it.")

    return JobRecord(
        job_id=job_id,
        source=source,
        title=title,
        company=company,
        location=location,
        description=description,
        posting_url=posting_url,
        apply_url=apply_url,
        posted_date=posted_date,
        discovered_date=_now_iso(),
        workplace_type=workplace_type,
        employment_type=employment_type,
        seniority_level=seniority_level,
        easy_apply=easy_apply,
        skills=skills,
    )


def _get_job_id(raw: dict[str, Any]) -> str:
    """Extract the job ID, trying multiple field name conventions."""
    # Prefer the explicit id field
    for key in ("id", "jobId", "job_id", "linkedInId", "linkedin_id"):
        val = raw.get(key)
        if val:
            return str(val).strip()

    # Fall back: extract numeric ID from the LinkedIn posting URL
    url = raw.get("link") or raw.get("url") or raw.get("jobUrl") or ""
    match = re.search(r"/jobs/view/(\d+)", str(url))
    if match:
        return match.group(1)

    return ""


def _get_company_name(raw: dict[str, Any]) -> str:
    """Company may be a nested dict {name, url} or a plain string field."""
    # Real scraper uses flat companyName field
    flat = raw.get("companyName") or raw.get("company_name") or ""
    if flat and isinstance(flat, str):
        return flat.strip()
    # Fixture uses nested company dict
    company = raw.get("company") or ""
    if isinstance(company, dict):
        return str(company.get("name") or "").strip()
    if isinstance(company, str):
        return company.strip()
    return ""


def _get_posting_url(raw: dict[str, Any], job_id: str) -> str:
    """Return a normalised posting URL."""
    # Real scraper uses 'link'; fixture uses 'url'
    url = _str(raw, "link", "url", "jobUrl", "postingUrl", "job_url") or ""
    if url:
        return _normalize_url(url)
    if job_id:
        return f"https://www.linkedin.com/jobs/view/{job_id}"
    return ""


def _get_workplace_type(raw: dict[str, Any]) -> str:
    """
    Real scraper returns workplaceTypes as a list e.g. ["Remote"].
    Fixture uses a plain string workplace_type.
    """
    types = raw.get("workplaceTypes")
    if isinstance(types, list) and types:
        return ", ".join(str(t) for t in types)
    return _str(raw, "workplace_type", "workplaceType", "workType") or ""


def _get_skills(raw: dict[str, Any]) -> list[str]:
    skills = raw.get("skills") or raw.get("requiredSkills") or []
    if isinstance(skills, list):
        return [str(s).strip() for s in skills if s]
    if isinstance(skills, str):
        return [s.strip() for s in skills.split(",") if s.strip()]
    return []


def _str(raw: dict[str, Any], *keys: str) -> str:
    """Return the first non-empty string value found for any of the given keys."""
    for key in keys:
        val = raw.get(key)
        if val and isinstance(val, str):
            return val.strip()
    return ""


def _parse_date(raw_date: str) -> str:
    """
    Try to parse a variety of date strings into ISO 8601 UTC.
    Returns the original string if parsing fails, empty string if input is empty.
    """
    if not raw_date:
        return ""
    raw_date = raw_date.strip()
    formats = [
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%B %d, %Y",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(raw_date, fmt)
            return dt.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    # Try dateutil as a fallback (installed via requirements.txt)
    try:
        from dateutil import parser as du_parser
        dt = du_parser.parse(raw_date)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        pass
    logger.debug("Could not parse date string: %r — keeping as-is", raw_date)
    return raw_date


def _normalize_url(url: str) -> str:
    """
    Strip tracking parameters and fragments from a URL so that
    two URLs pointing to the same job compare equal.
    Keeps only scheme + netloc + path.
    """
    try:
        parts = urlparse(url)
        clean = urlunparse((parts.scheme, parts.netloc, parts.path, "", "", ""))
        return clean.rstrip("/")
    except Exception:
        return url


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_dedup_key(source: str, job_id: str, posting_url: str) -> str:
    """
    Build a stable dedup key.
    Primary: source + job_id  (most reliable)
    Fallback: hash of normalised posting URL
    """
    if job_id:
        return f"{source}::{job_id}"
    if posting_url:
        url_hash = hashlib.sha256(posting_url.encode()).hexdigest()[:16]
        return f"{source}::url::{url_hash}"
    raise ValueError("Cannot create dedup key: no job_id and no posting_url")

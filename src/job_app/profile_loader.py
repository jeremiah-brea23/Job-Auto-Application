"""
src/job_app/profile_loader.py
──────────────────────────────
Loads and validates profile/profile.yaml into a typed Profile dataclass.
The assessor reads Profile at runtime — never hardcoded preferences.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

try:
    import yaml  # PyYAML
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False
    logger.warning("PyYAML not installed — profile loading unavailable. "
                   "Run: pip install pyyaml")


@dataclass
class Profile:
    # Role targeting
    target_roles: list[str]
    title_keywords_good: list[str]
    title_keywords_bad: list[str]

    # Location
    preferred_workplace_types: list[str]
    accepted_locations: list[str]

    # Employment type
    preferred_employment_types: list[str]
    reject_fulltime: bool
    accepted_seniority_levels: list[str]

    # Availability
    available_from: str
    available_until: str
    min_duration_weeks: int
    max_duration_weeks: int

    # Skills
    my_skills: dict[str, list[str]]  # {languages: [...], frameworks: [...], ...}

    # Deal-breakers
    description_red_flags: list[str]

    # Work authorization
    authorized_to_work_in_us: bool
    requires_sponsorship: bool

    # Résumé
    resume_path: str

    # AI
    use_ai_assessment: bool
    ai_assessment_model: str

    @property
    def all_skills(self) -> list[str]:
        """Flat list of all skills regardless of category."""
        result: list[str] = []
        for skills in self.my_skills.values():
            result.extend(skills)
        return [s.lower() for s in result]


def load_profile(profile_path: Path) -> Profile:
    """Load and return a Profile from the given YAML file."""
    if not _YAML_AVAILABLE:
        raise ImportError("PyYAML is required. Run: pip install pyyaml")

    if not profile_path.exists():
        raise FileNotFoundError(
            f"Profile not found: {profile_path}\n"
            f"Copy and edit profile/profile.yaml to match your background."
        )

    with profile_path.open(encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)

    logger.info("Loaded profile from %s", profile_path)

    return Profile(
        target_roles=_list(data, "target_roles"),
        title_keywords_good=_list(data, "title_keywords_good"),
        title_keywords_bad=_list(data, "title_keywords_bad"),
        preferred_workplace_types=_list(data, "preferred_workplace_types"),
        accepted_locations=_list(data, "accepted_locations"),
        preferred_employment_types=_list(data, "preferred_employment_types"),
        reject_fulltime=bool(data.get("reject_fulltime", False)),
        accepted_seniority_levels=[
            str(s) for s in data.get("accepted_seniority_levels", [])
        ],
        available_from=str(data.get("available_from", "")),
        available_until=str(data.get("available_until", "")),
        min_duration_weeks=int(data.get("min_duration_weeks", 8)),
        max_duration_weeks=int(data.get("max_duration_weeks", 16)),
        my_skills={
            k: [str(s) for s in v]
            for k, v in data.get("my_skills", {}).items()
        },
        description_red_flags=[
            s.lower() for s in _list(data, "description_red_flags")
        ],
        authorized_to_work_in_us=bool(data.get("authorized_to_work_in_us", True)),
        requires_sponsorship=bool(data.get("requires_sponsorship", False)),
        resume_path=str(data.get("resume_path", "resumes/resume.pdf")),
        use_ai_assessment=bool(data.get("use_ai_assessment", True)),
        ai_assessment_model=str(data.get("ai_assessment_model", "gpt-4o-mini")),
    )


def _list(data: dict[str, Any], key: str) -> list[str]:
    val = data.get(key, [])
    if isinstance(val, list):
        return [str(v) for v in val if v is not None]
    return []

"""
src/job_app/cover_letter_detector.py
──────────────────────────────────────
Determines whether a job application actually requires a cover letter.

Three possible states (matches the sheet column cover_letter_required):
  required  — the posting or application form explicitly asks for one
  optional  — the posting says it is optional / appreciated
  unknown   — cannot determine from the description alone

Important: a job description alone rarely gives a definitive answer.
The detector uses keyword heuristics against the description text and
returns "unknown" when unsure rather than guessing.

Application-form inspection (Phase 4) is the only reliable way to confirm
"required". Until then this module gives a best-effort signal.
"""

from __future__ import annotations

import re

# ── Keyword sets (all matched case-insensitively) ─────────────────────────

_REQUIRED_PATTERNS = [
    r"\bcover letter required\b",
    r"\bmust (include|submit|attach|provide|upload) (a )?cover letter\b",
    r"\bplease (include|submit|attach|provide|upload) (a )?cover letter\b",
    r"\bcover letter is required\b",
    r"\bapplication must include.*cover letter\b",
    r"\binclude a cover letter\b",
]

_OPTIONAL_PATTERNS = [
    r"\bcover letter (is )?(optional|welcomed|appreciated|encouraged|preferred)\b",
    r"\b(optional|encouraged).*cover letter\b",
    r"\bcover letter (not required|not mandatory)\b",
    r"\bno cover letter (required|needed|necessary)\b",
]

_MENTIONED_PATTERNS = [
    r"\bcover letter\b",
]


def detect_cover_letter_requirement(description: str) -> str:
    """
    Analyse the job description text and return one of:
      "required" | "optional" | "unknown"
    """
    if not description:
        return "unknown"

    text = description.lower()

    for pattern in _REQUIRED_PATTERNS:
        if re.search(pattern, text):
            return "required"

    for pattern in _OPTIONAL_PATTERNS:
        if re.search(pattern, text):
            return "optional"

    # Cover letter is mentioned but doesn't match required/optional patterns
    for pattern in _MENTIONED_PATTERNS:
        if re.search(pattern, text):
            return "unknown"

    # Not mentioned at all — still unknown (form may ask)
    return "unknown"

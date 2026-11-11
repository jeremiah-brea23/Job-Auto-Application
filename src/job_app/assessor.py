"""
src/job_app/assessor.py
────────────────────────
Phase 2 job fit assessment engine.

Two-stage approach:
  1. Rule-based fast pass  — transparent, deterministic, no API calls.
     Uses profile config (title keywords, seniority, workplace, red flags).
     Returns Good / Skip / Needs Review immediately.

  2. AI assessment         — for jobs that pass rules but need interpretation.
     Sends job description + résumé excerpt to the configured AI model.
     Returns a structured recommendation with reasons and uncertainties.
     Only called when use_ai_assessment=True in profile.yaml and
     AI_API_KEY is set in .env.

Output: AssessmentResult written back to Google Sheets columns
  fit_score, fit_reason, missing_requirements, application_status.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from src.job_app.normalizer import JobRecord
from src.job_app.profile_loader import Profile

logger = logging.getLogger(__name__)

FitScore = Literal["Good", "Maybe", "Skip", "Needs Review"]


@dataclass
class AssessmentResult:
    job_id: str
    title: str
    company: str
    fit_score: FitScore
    fit_reason: str               # Human-readable summary
    missing_requirements: str     # What the job needs that you may not have
    uncertainties: str            # Things that couldn't be determined
    suggested_status: str         # Recommended application_status value
    assessed_by: str              # "rules" | "ai" | "rules+ai"


class JobAssessor:
    """
    Assess a list of JobRecords against the user's Profile.

    Args:
        profile:      Loaded Profile from profile.yaml.
        resume_text:  Full text of the résumé (empty string = skip AI context).
        ai_api_key:   OpenAI-compatible API key (empty = rules only).
        ai_model:     Model identifier (e.g. "gpt-4o-mini").
    """

    def __init__(
        self,
        profile: Profile,
        resume_text: str = "",
        ai_api_key: str = "",
        ai_model: str = "gpt-4o-mini",
    ) -> None:
        self._profile = profile
        self._resume_text = resume_text
        self._ai_api_key = ai_api_key
        self._ai_model = ai_model
        self._ai_available = bool(ai_api_key) and profile.use_ai_assessment

    # ─────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────

    def assess(self, record: JobRecord) -> AssessmentResult:
        """Run the full assessment for a single JobRecord."""
        # Stage 1: rule-based
        result = self._rule_based(record)

        # Stage 2: AI — only for Good/Maybe from rules, and only if available
        if result.fit_score in ("Good", "Maybe") and self._ai_available:
            result = self._ai_assess(record, rule_result=result)

        logger.debug(
            "Assessment [%s] %s @ %s → %s (%s)",
            result.assessed_by, record.title, record.company,
            result.fit_score, result.fit_reason[:60],
        )
        return result

    def assess_many(self, records: list[JobRecord]) -> list[AssessmentResult]:
        """Assess a list of records. Already-assessed records can be filtered upstream."""
        results = []
        for record in records:
            results.append(self.assess(record))
        logger.info(
            "Assessed %d jobs — Good: %d, Maybe: %d, Skip: %d, Needs Review: %d",
            len(results),
            sum(1 for r in results if r.fit_score == "Good"),
            sum(1 for r in results if r.fit_score == "Maybe"),
            sum(1 for r in results if r.fit_score == "Skip"),
            sum(1 for r in results if r.fit_score == "Needs Review"),
        )
        return results

    # ─────────────────────────────────────────────────────────
    # Stage 1: Rule-based assessment
    # ─────────────────────────────────────────────────────────

    def _rule_based(self, record: JobRecord) -> AssessmentResult:
        p = self._profile
        reasons: list[str] = []
        missing: list[str] = []
        uncertainties: list[str] = []
        title_lower = record.title.lower()
        desc_lower = record.description.lower()

        # ── Hard disqualifiers ────────────────────────────────

        # Bad title keywords
        for kw in p.title_keywords_bad:
            if kw.lower() in title_lower:
                return self._make(record, "Skip",
                    f"Title contains disqualifying keyword: '{kw}'",
                    missing="", uncertainties="", by="rules")

        # Seniority level mismatch
        seniority = record.seniority_level.lower()
        accepted = [s.lower() for s in p.accepted_seniority_levels]
        if seniority and seniority not in accepted:
            # Allow empty seniority to pass
            return self._make(record, "Skip",
                f"Seniority level '{record.seniority_level}' not in accepted levels",
                missing="", uncertainties="", by="rules")

        # Employment type: reject full-time if configured
        emp_lower = record.employment_type.lower()
        if p.reject_fulltime and "full-time" in emp_lower:
            return self._make(record, "Skip",
                "Full-time role rejected per profile settings",
                missing="", uncertainties="", by="rules")

        # Description red flags
        for flag in p.description_red_flags:
            if flag in desc_lower:
                return self._make(record, "Needs Review",
                    f"Description contains red flag: '{flag}'",
                    missing="", uncertainties="", by="rules")

        # ── Positive signals ──────────────────────────────────
        good_signals = 0

        # Title has a good keyword
        for kw in p.title_keywords_good:
            if kw.lower() in title_lower:
                good_signals += 1
                reasons.append(f"Title matches '{kw}'")
                break

        # Preferred employment type
        for pref in p.preferred_employment_types:
            if pref.lower() in emp_lower:
                good_signals += 1
                reasons.append(f"Employment type '{record.employment_type}' matches preference")
                break

        # Preferred workplace type
        workplace_lower = record.workplace_type.lower()
        for pref in p.preferred_workplace_types:
            if pref.lower() in workplace_lower:
                good_signals += 1
                reasons.append(f"Workplace '{record.workplace_type}' matches preference")
                break
        else:
            if record.workplace_type:
                uncertainties.append(
                    f"Workplace type '{record.workplace_type}' not in preferred list"
                )

        # Skill overlap in description
        matched_skills = [
            s for s in p.all_skills
            if s in desc_lower
        ]
        if matched_skills:
            good_signals += 1
            top = matched_skills[:5]
            reasons.append(f"Skills mentioned in description: {', '.join(top)}")
        else:
            uncertainties.append("No matching skills detected in description")

        # ── Determine score ───────────────────────────────────
        if good_signals >= 3:
            score: FitScore = "Good"
        elif good_signals >= 1:
            score = "Maybe"
        else:
            score = "Needs Review"

        # Employment type not in preferred list
        pref_lower = [p_emp.lower() for p_emp in p.preferred_employment_types]
        if emp_lower and not any(p_emp in emp_lower for p_emp in pref_lower):
            missing.append(f"Employment type '{record.employment_type}' not preferred")
            if score == "Good":
                score = "Maybe"

        return self._make(
            record, score,
            fit_reason="; ".join(reasons) if reasons else "No strong positive signals",
            missing="; ".join(missing),
            uncertainties="; ".join(uncertainties),
            by="rules",
        )

    # ─────────────────────────────────────────────────────────
    # Stage 2: AI assessment
    # ─────────────────────────────────────────────────────────

    def _ai_assess(self, record: JobRecord, rule_result: AssessmentResult) -> AssessmentResult:
        """Call the AI model and return an enriched AssessmentResult."""
        try:
            response_text = self._call_ai(record)
            parsed = self._parse_ai_response(response_text)

            # Merge: AI can upgrade or downgrade the rule score
            final_score = parsed.get("fit_score", rule_result.fit_score)
            if final_score not in ("Good", "Maybe", "Skip", "Needs Review"):
                final_score = rule_result.fit_score

            # Combine rule reasons with AI reasons
            ai_reason = parsed.get("fit_reason", "")
            combined_reason = (
                f"[Rules] {rule_result.fit_reason} | [AI] {ai_reason}"
                if ai_reason else rule_result.fit_reason
            )

            return self._make(
                record,
                score=final_score,
                fit_reason=combined_reason,
                missing=parsed.get("missing_requirements", rule_result.missing_requirements),
                uncertainties=parsed.get("uncertainties", rule_result.uncertainties),
                by="rules+ai",
            )
        except Exception as exc:
            logger.warning("AI assessment failed for '%s': %s — using rule result",
                           record.title, exc)
            return rule_result

    def _call_ai(self, record: JobRecord) -> str:
        """Make the OpenAI API call and return raw response text."""
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError(
                "openai package required for AI assessment. "
                "Run: pip install openai"
            )

        client = OpenAI(api_key=self._ai_api_key)
        p = self._profile

        resume_excerpt = self._resume_text[:3000] if self._resume_text else "(not provided)"

        prompt = f"""You are a job fit assessor. Evaluate whether this job matches the candidate.

CANDIDATE PROFILE:
- Target roles: {', '.join(p.target_roles)}
- Skills: {', '.join(p.all_skills[:20])}
- Preferred workplace: {', '.join(p.preferred_workplace_types)}
- Available: {p.available_from} to {p.available_until}
- Résumé excerpt:
{resume_excerpt}

JOB POSTING:
- Title: {record.title}
- Company: {record.company}
- Location: {record.location}
- Workplace: {record.workplace_type}
- Employment: {record.employment_type}
- Seniority: {record.seniority_level}
- Description:
{record.description[:2000]}

Respond in this exact format (one line each, no extra text):
FIT_SCORE: <Good|Maybe|Skip|Needs Review>
FIT_REASON: <one sentence explaining the main reason>
MISSING_REQUIREMENTS: <what the job requires that the candidate may lack, or "None">
UNCERTAINTIES: <what you could not determine, or "None">

Rules:
- Use only facts from the posting and résumé. Do not assume.
- If the posting has red flags (commission-only, MLM, unpaid, requires 3+ years), score Skip.
- If the role is not an internship or entry-level, score Skip.
- Treat unknown information as unknown — say so in UNCERTAINTIES."""

        response = client.chat.completions.create(
            model=self._ai_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=300,
        )
        return response.choices[0].message.content or ""

    def _parse_ai_response(self, text: str) -> dict[str, str]:
        """Parse the structured AI response into a dict."""
        result: dict[str, str] = {}
        patterns = {
            "fit_score": r"FIT_SCORE:\s*(.+)",
            "fit_reason": r"FIT_REASON:\s*(.+)",
            "missing_requirements": r"MISSING_REQUIREMENTS:\s*(.+)",
            "uncertainties": r"UNCERTAINTIES:\s*(.+)",
        }
        for key, pattern in patterns.items():
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                result[key] = match.group(1).strip()
        return result

    # ─────────────────────────────────────────────────────────
    # Helper
    # ─────────────────────────────────────────────────────────

    def _make(
        self,
        record: JobRecord,
        score: FitScore,
        fit_reason: str,
        missing: str,
        uncertainties: str,
        by: str,
    ) -> AssessmentResult:
        status_map: dict[FitScore, str] = {
            "Good": "Ready",
            "Maybe": "Needs Review",
            "Skip": "Skipped",
            "Needs Review": "Needs Review",
        }
        return AssessmentResult(
            job_id=record.job_id,
            title=record.title,
            company=record.company,
            fit_score=score,
            fit_reason=fit_reason,
            missing_requirements=missing,
            uncertainties=uncertainties,
            suggested_status=status_map[score],
            assessed_by=by,
        )

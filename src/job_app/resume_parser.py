"""
src/job_app/resume_parser.py
─────────────────────────────
Extracts plain text from your résumé file so the assessor can read it.

Supported formats:
  .pdf   → pdfplumber (better than pypdf for text-heavy résumés)
  .docx  → python-docx
  .txt   → read directly

The parsed text is used only for AI assessment context.
It is never logged in full and never sent to external services
other than the configured AI model API.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def parse_resume(resume_path: Path) -> str:
    """
    Return the plain-text content of the résumé at resume_path.
    Raises FileNotFoundError if the file doesn't exist.
    Raises ValueError for unsupported formats.
    """
    if not resume_path.exists():
        raise FileNotFoundError(
            f"Résumé not found: {resume_path}\n"
            f"Place your résumé at that path, or update 'resume_path' in profile/profile.yaml."
        )

    suffix = resume_path.suffix.lower()

    if suffix == ".pdf":
        return _parse_pdf(resume_path)
    elif suffix == ".docx":
        return _parse_docx(resume_path)
    elif suffix in (".txt", ".md"):
        return resume_path.read_text(encoding="utf-8")
    else:
        raise ValueError(
            f"Unsupported résumé format: {suffix}. "
            f"Supported: .pdf, .docx, .txt, .md"
        )


def _parse_pdf(path: Path) -> str:
    try:
        import pdfplumber
    except ImportError:
        raise ImportError(
            "pdfplumber is required to parse PDF résumés.\n"
            "Run: pip install pdfplumber"
        )

    text_parts: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)

    full_text = "\n".join(text_parts).strip()
    logger.info("Parsed PDF résumé: %d characters from %d page(s)",
                len(full_text), len(text_parts))
    return full_text


def _parse_docx(path: Path) -> str:
    try:
        from docx import Document
    except ImportError:
        raise ImportError(
            "python-docx is required to parse DOCX résumés.\n"
            "Run: pip install python-docx"
        )

    doc = Document(str(path))
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    full_text = "\n".join(paragraphs)
    logger.info("Parsed DOCX résumé: %d characters", len(full_text))
    return full_text

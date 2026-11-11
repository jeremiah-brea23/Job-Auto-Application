"""
cli.py
───────
Entry point for the Job Application AI System.

Usage examples:

  # Test with built-in fixture (no credentials needed)
  python cli.py import --fixture

  # Test with a custom fixture file
  python cli.py import --fixture fixtures/sample_jobs.json

  # Import from an existing Apify dataset
  python cli.py import --dataset-id YOUR_DATASET_ID

  # Trigger a saved Apify Task and import its results
  python cli.py import --task-id YOUR_TASK_ID

  # Dry-run any of the above (shows what would happen, writes nothing)
  python cli.py import --fixture --dry-run
  python cli.py import --dataset-id YOUR_DATASET_ID --dry-run

  # Inspect the first N raw items from a source (helpful for field discovery)
  python cli.py inspect --dataset-id YOUR_DATASET_ID --limit 3
  python cli.py inspect --fixture --limit 2
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import click
import os

# ── Logging setup ────────────────────────────────────────────────────────────
# Configure before any project imports so all loggers pick up the handler.

def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


# ── Default paths ─────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent
_DEFAULT_FIXTURE = _PROJECT_ROOT / "fixtures" / "sample_jobs.json"


# ─────────────────────────────────────────────────────────────────────────────
# CLI root group
# ─────────────────────────────────────────────────────────────────────────────

@click.group()
@click.option("--verbose", "-v", is_flag=True, default=False,
              help="Enable DEBUG-level logging.")
@click.pass_context
def cli(ctx: click.Context, verbose: bool) -> None:
    """Job Application AI System – Phase 1: Import & Track."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    _setup_logging(verbose)


# ─────────────────────────────────────────────────────────────────────────────
# import command
# ─────────────────────────────────────────────────────────────────────────────

@cli.command("import")
@click.option("--fixture", "use_fixture", is_flag=True, default=False,
              help="Import from the local fixture file (no Apify credentials needed).")
@click.option("--fixture-path", type=click.Path(exists=True, path_type=Path),
              default=None,
              help=f"Path to a custom fixture JSON (default: {_DEFAULT_FIXTURE}).")
@click.option("--dataset-id", default=None,
              help="Apify dataset ID to read (overrides APIFY_DATASET_ID in .env).")
@click.option("--task-id", default=None,
              help="Saved Apify Task ID to run and fetch (overrides APIFY_TASK_ID in .env).")
@click.option("--dry-run", is_flag=True, default=False,
              help="Show what would be imported without writing to Google Sheets.")
@click.pass_context
def import_jobs(
    ctx: click.Context,
    use_fixture: bool,
    fixture_path: Path | None,
    dataset_id: str | None,
    task_id: str | None,
    dry_run: bool,
) -> None:
    """
    Fetch jobs from a source, normalize them, deduplicate, and write to Google Sheets.
    """
    from config.settings import get_settings
    from src.job_app.pipeline import ImportPipeline
    from src.job_app.sheets_client import SheetsClient

    # Exactly one source must be specified
    sources_given = sum([use_fixture, bool(dataset_id), bool(task_id)])
    if sources_given == 0:
        raise click.UsageError(
            "Specify exactly one source: --fixture, --dataset-id, or --task-id.\n"
            "Run 'python cli.py import --help' for details."
        )
    if sources_given > 1:
        raise click.UsageError(
            "Specify only one source at a time: --fixture, --dataset-id, or --task-id."
        )

    cfg = get_settings(dry_run=dry_run)

    # In fixture mode we don't need Google creds when dry-running
    sheets = SheetsClient(
        service_account_json=cfg.google_service_account_json,
        spreadsheet_id=cfg.google_spreadsheet_id,
        worksheet_name=cfg.google_worksheet_name,
        dry_run=dry_run,
    )

    pipeline = ImportPipeline(
        sheets_client=sheets,
        apify_token=cfg.apify_token if not use_fixture else "",
        dry_run=dry_run,
    )

    if dry_run:
        click.echo("⚠️  DRY RUN — nothing will be written to Google Sheets.\n")

    if use_fixture:
        path = fixture_path or _DEFAULT_FIXTURE
        click.echo(f"📂 Source: fixture  →  {path}")
        result = pipeline.run_from_fixture(path)
    elif dataset_id:
        resolved_id = dataset_id or cfg.apify_dataset_id
        click.echo(f"☁️  Source: Apify dataset  →  {resolved_id}")
        result = pipeline.run_from_dataset(resolved_id)
    else:
        resolved_task = task_id or cfg.apify_task_id
        click.echo(f"⚙️  Source: Apify task  →  {resolved_task}")
        result = pipeline.run_from_task(resolved_task)

    # ── Print summary table ───────────────────────────────────
    click.echo("")
    click.echo("─" * 45)
    click.echo("  Import Summary")
    click.echo("─" * 45)
    click.echo(f"  Fetched from source :  {result.total_fetched}")
    click.echo(f"  Normalized          :  {result.total_normalized}")
    click.echo(f"  Normalization fails :  {result.total_skipped}")
    click.echo(f"  Duplicates skipped  :  {result.total_duplicates}")
    click.echo(f"  New rows written    :  {result.total_new}")
    if result.dry_run:
        click.echo("  (DRY RUN — no actual writes)")
    click.echo("─" * 45)

    if result.total_new > 0 and not result.dry_run:
        click.echo(f"\n✅  {result.total_new} job(s) added to your tracking sheet.")
    elif result.total_new > 0 and result.dry_run:
        click.echo(f"\n🔍  {result.total_new} job(s) would be added (dry run).")
    elif result.total_duplicates == result.total_normalized:
        click.echo("\nℹ️  All jobs already tracked — nothing new to add.")
    else:
        click.echo("\n⚠️  No new jobs were added. Check logs for details.")


# ─────────────────────────────────────────────────────────────────────────────
# assess command
# ─────────────────────────────────────────────────────────────────────────────

@cli.command("assess")
@click.option("--profile-path", type=click.Path(path_type=Path),
              default=None,
              help="Path to profile YAML (default: profile/profile.yaml).")
@click.option("--resume-path", type=click.Path(path_type=Path),
              default=None,
              help="Path to résumé file (PDF/DOCX/TXT). Skips AI context if omitted.")
@click.option("--limit", default=None, type=int,
              help="Max number of jobs to assess in this run.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Show assessment results without writing back to the sheet.")
@click.option("--rules-only", is_flag=True, default=False,
              help="Skip AI assessment and use rule-based scoring only.")
@click.pass_context
def assess_jobs(
    ctx: click.Context,
    profile_path: Path | None,
    resume_path: Path | None,
    limit: int | None,
    dry_run: bool,
    rules_only: bool,
) -> None:
    """
    Assess unscored jobs in the sheet for fit against your profile.
    Writes fit_score, fit_reason, missing_requirements, and application_status.
    """
    from config.settings import get_settings
    from src.job_app.pipeline import ImportPipeline
    from src.job_app.sheets_client import SheetsClient

    cfg = get_settings(dry_run=dry_run)

    resolved_profile = profile_path or (_PROJECT_ROOT / "profile" / "profile.yaml")
    resolved_resume = resume_path or (_PROJECT_ROOT / cfg.resume_path
                                      if hasattr(cfg, "resume_path") else None)

    # Load profile to find resume_path if not explicitly given
    if not resolved_resume:
        try:
            import yaml
            with open(resolved_profile, encoding="utf-8") as fh:
                p = yaml.safe_load(fh)
            resolved_resume = _PROJECT_ROOT / p.get("resume_path", "resumes/resume.pdf")
        except Exception:
            pass

    sheets = SheetsClient(
        service_account_json=cfg.google_service_account_json,
        spreadsheet_id=cfg.google_spreadsheet_id,
        worksheet_name=cfg.google_worksheet_name,
        dry_run=dry_run,
    )

    pipeline = ImportPipeline(
        sheets_client=sheets,
        apify_token=cfg.apify_token,
        dry_run=dry_run,
    )

    ai_key = "" if rules_only else cfg.ai_api_key

    if dry_run:
        click.echo("⚠️  DRY RUN — scores will not be written to Google Sheets.\n")
    if rules_only or not ai_key:
        click.echo("ℹ️  Running rules-only assessment (no AI).\n")
    else:
        click.echo(f"🤖 AI assessment enabled (model: {cfg.ai_model}).\n")

    click.echo(f"📋 Profile: {resolved_profile}")
    if resolved_resume and Path(resolved_resume).exists():
        click.echo(f"📄 Résumé:  {resolved_resume}")
    else:
        click.echo("📄 Résumé:  not found — AI will run without résumé context")
    click.echo("")

    result = pipeline.run_assessment(
        profile_path=resolved_profile,
        resume_path=Path(resolved_resume) if resolved_resume else None,
        ai_api_key=ai_key,
        ai_model=cfg.ai_model,
        limit=limit,
    )

    click.echo("")
    click.echo("─" * 45)
    click.echo("  Assessment Summary")
    click.echo("─" * 45)
    click.echo(f"  Total rows in sheet  :  {result.total_rows}")
    click.echo(f"  Already assessed     :  {result.already_assessed}")
    click.echo(f"  Assessed this run    :  {result.assessed_now}")
    click.echo(f"  ── Scores ──────────────────────────────")
    click.echo(f"  Good                 :  {result.score_breakdown.get('Good', 0)}")
    click.echo(f"  Maybe                :  {result.score_breakdown.get('Maybe', 0)}")
    click.echo(f"  Needs Review         :  {result.score_breakdown.get('Needs Review', 0)}")
    click.echo(f"  Skip                 :  {result.score_breakdown.get('Skip', 0)}")
    if result.dry_run:
        click.echo("  (DRY RUN — no actual writes)")
    click.echo("─" * 45)


# ─────────────────────────────────────────────────────────────────────────────
# cover-letters command
# ─────────────────────────────────────────────────────────────────────────────

@cli.command("cover-letters")
@click.option("--profile-path", type=click.Path(path_type=Path), default=None,
              help="Path to profile YAML (default: profile/profile.yaml).")
@click.option("--resume-path", type=click.Path(path_type=Path), default=None,
              help="Path to résumé file. Reads from profile.yaml if not set.")
@click.option("--base-letter", type=click.Path(path_type=Path), default=None,
              help="Path to base cover letter (default: cover_letters/base_cover_letter.txt).")
@click.option("--output-dir", type=click.Path(path_type=Path), default=None,
              help="Directory to save generated letters (default: cover_letters/generated).")
@click.option("--drive-folder-id", default=None,
              help="Google Drive folder ID to upload letters into.")
@click.option("--docx", "produce_docx", is_flag=True, default=False,
              help="Also produce a .docx file for each letter.")
@click.option("--detect-only", is_flag=True, default=False,
              help="Only detect cover letter requirements — do not generate letters.")
@click.option("--limit", default=None, type=int,
              help="Max number of jobs to process in this run.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Show what would happen without writing anything.")
@click.option("--name", "candidate_name", default="",
              help="Your full name for the letter sign-off.")
@click.pass_context
def cover_letters_cmd(
    ctx: click.Context,
    profile_path: Path | None,
    resume_path: Path | None,
    base_letter: Path | None,
    output_dir: Path | None,
    drive_folder_id: str | None,
    produce_docx: bool,
    detect_only: bool,
    limit: int | None,
    dry_run: bool,
    candidate_name: str,
) -> None:
    """
    Detect cover letter requirements and generate tailored letters for eligible jobs.

    Eligible jobs: application_status is Ready or Maybe, and no letter exists yet.
    Requires AI_API_KEY in .env for generation (detection works without it).
    """
    from config.settings import get_settings
    from src.job_app.pipeline import ImportPipeline
    from src.job_app.sheets_client import SheetsClient

    cfg = get_settings(dry_run=dry_run)

    resolved_profile = profile_path or (_PROJECT_ROOT / "profile" / "profile.yaml")

    # Load resume_path from profile if not explicitly given
    if not resume_path:
        try:
            import yaml
            with open(resolved_profile, encoding="utf-8") as fh:
                p_data = yaml.safe_load(fh)
            resume_path = _PROJECT_ROOT / p_data.get("resume_path", "resumes/resume.pdf")
        except Exception:
            pass

    resolved_base = base_letter or (_PROJECT_ROOT / "cover_letters" / "base_cover_letter.txt")
    resolved_output = output_dir or (_PROJECT_ROOT / "cover_letters" / "generated")
    resolved_drive_folder = drive_folder_id or os.getenv("GOOGLE_DRIVE_FOLDER_ID", "")

    sheets = SheetsClient(
        service_account_json=cfg.google_service_account_json,
        spreadsheet_id=cfg.google_spreadsheet_id,
        worksheet_name=cfg.google_worksheet_name,
        dry_run=dry_run,
    )

    pipeline = ImportPipeline(
        sheets_client=sheets,
        apify_token=cfg.apify_token,
        dry_run=dry_run,
    )

    ai_key = "" if detect_only else cfg.ai_api_key

    if dry_run:
        click.echo("⚠️  DRY RUN — nothing will be written.\n")
    if detect_only:
        click.echo("🔍  Detection only — letters will not be generated.\n")
    elif not ai_key:
        click.echo("⚠️  AI_API_KEY not set — running detection only.\n")
        click.echo("    Set AI_API_KEY in .env to enable letter generation.\n")

    click.echo(f"📋 Profile : {resolved_profile}")
    click.echo(f"📄 Résumé  : {resume_path}")
    click.echo(f"✉️  Base    : {resolved_base}")
    click.echo(f"📁 Output  : {resolved_output}")
    click.echo("")

    result = pipeline.run_cover_letters(
        profile_path=resolved_profile,
        resume_path=Path(resume_path) if resume_path else None,
        base_letter_path=resolved_base,
        output_dir=resolved_output,
        ai_api_key=ai_key,
        ai_model=cfg.ai_model,
        candidate_name=candidate_name,
        service_account_json=cfg.google_service_account_json if not detect_only else None,
        drive_folder_id=resolved_drive_folder,
        produce_docx=produce_docx,
        limit=limit,
    )

    click.echo("")
    click.echo("─" * 45)
    click.echo("  Cover Letter Summary")
    click.echo("─" * 45)
    click.echo(f"  Eligible jobs        :  {result.total_eligible}")
    click.echo(f"  Already have letter  :  {result.already_have_letter}")
    click.echo(f"  Requirement detected :  {result.detection_only}")
    click.echo(f"  Letters generated    :  {result.generated}")
    click.echo(f"  Uploaded to Drive    :  {result.uploaded}")
    click.echo(f"  Flagged for review   :  {result.flagged_for_review}")
    if result.dry_run:
        click.echo("  (DRY RUN — no actual writes)")
    click.echo("─" * 45)


# ─────────────────────────────────────────────────────────────────────────────
# inspect command  (field discovery helper)
# ─────────────────────────────────────────────────────────────────────────────

@cli.command("inspect")
@click.option("--fixture", "use_fixture", is_flag=True, default=False,
              help="Inspect the local fixture file.")
@click.option("--fixture-path", type=click.Path(exists=True, path_type=Path),
              default=None, help="Custom fixture path.")
@click.option("--dataset-id", default=None,
              help="Apify dataset ID to inspect.")
@click.option("--limit", default=2, show_default=True,
              help="Number of items to print.")
@click.pass_context
def inspect_source(
    ctx: click.Context,
    use_fixture: bool,
    fixture_path: Path | None,
    dataset_id: str | None,
    limit: int,
) -> None:
    """
    Print raw items from a source to inspect actual field names and structure.
    Useful before adding a new scraper or debugging normalization issues.
    """
    import json

    if use_fixture:
        path = fixture_path or _DEFAULT_FIXTURE
        with path.open(encoding="utf-8") as fh:
            items = json.load(fh)
        click.echo(f"Fixture: {path}  ({len(items)} items total)\n")
    elif dataset_id:
        from config.settings import get_settings
        from src.job_app.apify_client import ApifyJobClient
        cfg = get_settings()
        client = ApifyJobClient(cfg.apify_token)
        items = client.read_dataset(dataset_id)
        click.echo(f"Dataset {dataset_id}  ({len(items)} items total)\n")
    else:
        raise click.UsageError("Specify --fixture or --dataset-id.")

    for i, item in enumerate(items[:limit], 1):
        click.echo(f"── Item {i} ─────────────────────────────────────────")
        click.echo(json.dumps(item, indent=2, ensure_ascii=False, default=str))
        click.echo("")


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli()

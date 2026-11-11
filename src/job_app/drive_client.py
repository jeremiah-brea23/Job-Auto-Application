"""
src/job_app/drive_client.py
────────────────────────────
Uploads cover letter files to a Google Drive folder and returns
a shareable view link to record in the tracking sheet.

Uses the same service account credentials as the Sheets client.
The service account needs "Editor" or "Contributor" access to the
Drive folder (or the shared drive if applicable).

If GOOGLE_DRIVE_FOLDER_ID is not set, files are uploaded to the
service account's root Drive (visible only to the service account).
Share the folder with your personal Gmail to view them.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
]


class DriveClient:
    """
    Upload files to Google Drive using a service account.

    Args:
        service_account_json: Path to the service account JSON key file.
        folder_id:            Google Drive folder ID to upload into.
                              If empty, uploads to the service account root.
    """

    def __init__(
        self,
        service_account_json: Path,
        folder_id: str = "",
    ) -> None:
        self._folder_id = folder_id
        self._service = self._build_service(service_account_json)

    # ─────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────

    def upload(self, file_path: Path, mime_type: str | None = None) -> str:
        """
        Upload a file to Drive and return its shareable view URL.

        Args:
            file_path: Local path to the file.
            mime_type: MIME type override. Auto-detected from extension if None.

        Returns:
            Shareable Google Drive view URL for the uploaded file.
        """
        from googleapiclient.http import MediaFileUpload

        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        detected_mime = mime_type or _mime_for(file_path)
        file_metadata: dict = {"name": file_path.name}
        if self._folder_id:
            file_metadata["parents"] = [self._folder_id]

        media = MediaFileUpload(str(file_path), mimetype=detected_mime, resumable=False)

        logger.info("Uploading %s to Drive …", file_path.name)
        result = (
            self._service.files()
            .create(
                body=file_metadata,
                media_body=media,
                fields="id, webViewLink",
            )
            .execute()
        )

        file_id = result.get("id", "")
        view_link = result.get("webViewLink", "")

        if not view_link and file_id:
            # Fallback: construct the view link from the file ID
            view_link = f"https://drive.google.com/file/d/{file_id}/view"

        logger.info("Uploaded to Drive: %s", view_link)
        return view_link

    def upload_txt_as_doc(self, file_path: Path) -> str:
        """
        Upload a .txt file and convert it to a Google Doc automatically.
        Returns the Google Docs view link.
        """
        from googleapiclient.http import MediaFileUpload

        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        file_metadata: dict = {
            "name": file_path.stem,  # Doc name without .txt extension
            "mimeType": "application/vnd.google-apps.document",
        }
        if self._folder_id:
            file_metadata["parents"] = [self._folder_id]

        media = MediaFileUpload(str(file_path), mimetype="text/plain", resumable=False)

        logger.info("Uploading %s to Drive as Google Doc …", file_path.name)
        result = (
            self._service.files()
            .create(
                body=file_metadata,
                media_body=media,
                fields="id, webViewLink",
            )
            .execute()
        )

        file_id = result.get("id", "")
        view_link = result.get("webViewLink", "")
        if not view_link and file_id:
            view_link = f"https://docs.google.com/document/d/{file_id}/edit"

        logger.info("Uploaded Google Doc: %s", view_link)
        return view_link

    # ─────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────

    def _build_service(self, service_account_json: Path):
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        if not service_account_json.exists():
            raise FileNotFoundError(
                f"Service account JSON not found: {service_account_json}\n"
                f"See docs/SETUP_GOOGLE.md"
            )

        creds = Credentials.from_service_account_file(
            str(service_account_json),
            scopes=_SCOPES + ["https://www.googleapis.com/auth/drive"],
        )
        return build("drive", "v3", credentials=creds, cache_discovery=False)


def _mime_for(path: Path) -> str:
    mime_map = {
        ".txt": "text/plain",
        ".pdf": "application/pdf",
        ".docx": (
            "application/vnd.openxmlformats-officedocument"
            ".wordprocessingml.document"
        ),
    }
    return mime_map.get(path.suffix.lower(), "application/octet-stream")

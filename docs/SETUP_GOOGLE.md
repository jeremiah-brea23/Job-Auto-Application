# Setting up Google Sheets access

Phase 1 writes job records to a Google Sheet using a **service account** — a
machine identity that doesn't require interactive login.  Follow these steps
once; the credentials file you download is never committed to Git.

---

## 1 — Create a Google Cloud project

1. Go to <https://console.cloud.google.com/>.
2. Click the project selector at the top → **New Project**.
3. Name it (e.g. `job-app-ai`) and click **Create**.

---

## 2 — Enable the required APIs

In your new project:

1. Go to **APIs & Services → Library**.
2. Search for and enable **Google Sheets API**.
3. Search for and enable **Google Drive API**.

---

## 3 — Create a service account and download the key

1. Go to **APIs & Services → Credentials**.
2. Click **Create Credentials → Service Account**.
3. Give it a name (e.g. `job-app-sheets`) and click **Create and Continue**.
4. Skip the optional role and user-access steps → click **Done**.
5. Click the service account you just created → **Keys** tab.
6. Click **Add Key → Create new key → JSON → Create**.
7. The JSON file downloads automatically.  Move it to:

   ```
   Auto Job application/credentials/google_service_account.json
   ```

   The `credentials/` folder is in `.gitignore` — it will never be committed.

8. Copy the **client_email** field from that JSON file (looks like
   `job-app-sheets@your-project.iam.gserviceaccount.com`).

---

## 4 — Create your tracking spreadsheet

1. Go to <https://sheets.google.com> and create a new blank spreadsheet.
2. Name it (e.g. `Job Application Tracker`).
3. Copy the **Spreadsheet ID** from the URL:

   ```
   https://docs.google.com/spreadsheets/d/<SPREADSHEET_ID>/edit
   ```

4. Share the spreadsheet with the service account email from step 3:
   - Click **Share** (top-right).
   - Paste the service account email.
   - Set role to **Editor**.
   - Uncheck "Notify people".
   - Click **Share**.

---

## 5 — Configure your .env

Open `.env` (copy from `.env.example` if you haven't already) and set:

```
GOOGLE_SERVICE_ACCOUNT_JSON=credentials/google_service_account.json
GOOGLE_SPREADSHEET_ID=<your spreadsheet ID from step 4>
GOOGLE_WORKSHEET_NAME=Jobs
```

---

## 6 — Verify

Run the dry-run import with the fixture to confirm Sheets access works:

```powershell
python cli.py import --fixture --dry-run
```

Then run without `--dry-run` to write the fixture jobs to your sheet:

```powershell
python cli.py import --fixture
```

Open your spreadsheet — you should see a header row and 5 job rows.

---

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `FileNotFoundError: credentials/google_service_account.json` | Key file not in place | Move the downloaded JSON to `credentials/` and rename it |
| `gspread.exceptions.APIError: 403` | Sheet not shared with service account | Share the sheet with the service account email (step 4) |
| `gspread.exceptions.SpreadsheetNotFound` | Wrong spreadsheet ID | Double-check the ID in your `.env` |
| `google.auth.exceptions.DefaultCredentialsError` | JSON key is malformed | Re-download the key from Google Cloud Console |

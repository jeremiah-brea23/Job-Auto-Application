# Setting up Apify access

The system reads from your existing LinkedIn scraper run and can also trigger
new runs of a saved Apify Task.

---

## 1 — Get your Apify API token

1. Log in to <https://console.apify.com/>.
2. Click your avatar → **Settings** → **Integrations**.
3. Copy your **Personal API token**.
4. Add it to `.env`:

   ```
   APIFY_TOKEN=apify_api_xxxxxxxxxxxxxxxxxxxxxxxx
   ```

---

## 2 — Find your existing dataset ID

From your successful LinkedIn scraper run:

1. In Apify Console, go to **Actors → Runs** (or find the run in your saved
   task's history).
2. Click the completed run.
3. In the run detail page, look for **Default dataset** — click the link.
4. The dataset ID is in the URL:

   ```
   https://console.apify.com/storage/datasets/<DATASET_ID>
   ```

5. Add it to `.env`:

   ```
   APIFY_DATASET_ID=your_dataset_id_here
   ```

---

## 3 — (Optional) Find your saved Task ID

If you saved your scraper configuration as an Apify Task and want to trigger
new runs automatically:

1. Go to **Saved Tasks** in Apify Console.
2. Click your LinkedIn scraper task.
3. The Task ID is in the URL:

   ```
   https://console.apify.com/actors/tasks/<TASK_ID>
   ```

4. Add it to `.env`:

   ```
   APIFY_TASK_ID=your_task_id_here
   ```

---

## Usage

```powershell
# Read your existing dataset (fastest, no new Apify compute)
python cli.py import --dataset-id YOUR_DATASET_ID

# Or use the value from .env
python cli.py import --dataset-id

# Trigger a new scraper run and import the results
python cli.py import --task-id YOUR_TASK_ID

# Inspect raw field names from your dataset (2 items)
python cli.py inspect --dataset-id YOUR_DATASET_ID --limit 2
```

The `inspect` command is useful if the normalizer misses any fields — it
shows you the exact JSON structure Apify returns so you can update the
field aliases in `src/job_app/normalizer.py`.

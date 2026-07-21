# Zetheta Submission Resync CLI

A dependency-free Python 3 CLI that:

1. Fetches the **tech** submissions list from `https://www.zetheta.com/wp-json/v1/submissions`
2. Fetches the **non-tech** submissions list from `https://www.zetheta.com/wp-json/v1/submissions/?type=nontech`
3. Builds each user's report JSON URI from the submission data
4. Fetches the report JSON to extract the `percentage` score
5. Calls the `update_submissions` API for each record

## Usage

Run each command separately. The `#` comments above them are just descriptions, not part of the command.

```bash
# Tech + non-tech, real run (replace with your actual app code)
python3 resync.py --app-code "YOUR_APP_CODE_HERE"

# Or put the app code in a .env file:
cp .env.example .env
# edit .env, then run:
python3 resync.py

# Dry run: fetch reports and build payloads but do not POST
python3 resync.py --app-code "YOUR_APP_CODE_HERE" --dry-run

# Test mode: validate endpoints and a few sample report URLs only
python3 resync.py --test

# Process only non-tech, with a limit and verbose per-record logging
python3 resync.py --app-code "YOUR_APP_CODE_HERE" --type nontech --limit 50 --verbose

# Adjust concurrency and write a summary CSV
python3 resync.py --app-code "YOUR_APP_CODE_HERE" --workers 3 --output results.csv
```

## CLI options

| Flag | Description |
|------|-------------|
| `--app-code` | App code / API key for the `update_submissions` endpoint. |
| `--env-file` | Path to a `.env` file containing `APP_CODE` (default: `.env` if present). |
| `--type` | `tech`, `nontech`, or `all` (default: `all`). |
| `--limit` | Process only the first N records per type. |
| `--workers` | Concurrent worker threads (default: 5). Lower this if the API rate-limits you. |
| `--dry-run` | Fetch reports and build payloads but do not POST to the API. |
| `--test` | Validate connectivity and sample report URLs without updating anything. |
| `--output` | Path to write a CSV summary of all processed records. |
| `--verbose` | Show per-record debug output instead of only progress summaries. |

## Report URI construction

- **Tech (primary):** `https://zetheta-reports.s3.ap-south-1.amazonaws.com/reports/{user_id}/{course_number}_{course_name_underscore_separated}_result.json`
- **Non-tech:** `https://zetheta-reports.s3.ap-south-1.amazonaws.com/non-tech-reports/{user_id}/report_{user_id}_{course_number}_{course_name_underscore_separated}.json`

Spaces in `course_name` are replaced with underscores; existing underscores and hyphens are kept as-is.

### Fallback for tech submissions

If a tech submission's report does not exist under the tech path, the CLI automatically tries the **non-tech** report path as a fallback. This handles cases where submissions were originally categorised as non-tech and their reports were generated there. The `report_uri` in the update payload is set to whichever URL actually resolved.

## Update API payload

Each successful POST contains:

```json
{
  "app_code": "YOUR_APP_CODE_HERE",
  "user_id": <user_id>,
  "course_id": <course_id>,
  "role_id": <role_id>,
  "report_pdf": "",
  "report_uri": "<constructed S3 report JSON URI>",
  "percentage": <score from report JSON>
}
```

## Output and logging

The CLI prints each step to stdout with timestamps and log levels:

- `[FETCH]` — downloading a submissions list
- `[PROCESS]` — starting the record loop
- `[PROGRESS]` — processed count (non-verbose mode)
- `[RECORD]` — per-record status (verbose mode)
- `[FALLBACK]` — tech record resolved via non-tech report URL
- `[PAYLOAD]` — payload that would be POSTed (verbose dry-run mode)
- `[API]` — full response from the update_submissions API call
- `[RETRY]` — transient 5xx / timeout retry
- `[SUMMARY]` — status counts per submission type
- `[DONE]` — final totals and breakdown
- `[CSV]` — CSV summary written
- `[TEST]` — validation mode output

Skipped records are logged when the report JSON is missing or inaccessible, or when the score is not found.

## Running the validation suite

A quick, non-destructive test script is included:

```bash
bash run_tests.sh
```

It checks syntax, help output, endpoint connectivity, sample report URLs, and a small dry-run without posting any updates.

## Notes

- The script is dependency-free: it only uses Python 3 standard libraries.
- The tech submissions list is large (~8 MB), so fetching it may take 30–40 seconds. The CLI uses a 120-second timeout and retries for that fetch.
- Some report JSONs may not exist or return 403/404; those records are skipped and logged.
- Transient 5xx / network errors are retried up to 3 times with exponential backoff.
- If the update API returns a 2xx status with an empty response body, the script treats it as a success instead of an error.

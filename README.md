# Notion Backup Exporter

Daily, fully-automated backup of a Notion workspace to git. Uses Notion's own "Export workspace content" flow (cookie-authenticated, the same artifact you'd get from clicking *Settings → Export* manually) and publishes the unpacked Markdown/CSV/HTML tree to a dedicated git branch.

It is not affiliated with or endorsed by Notion. The export path uses Notion's internal API endpoints (the same ones the Notion web app uses); they are undocumented and may change.

- Daily **official Notion export ZIP** download (cookie-auth via `token_v2` + `file_token`)
- Export URL retrieved from Notion's notifications API (post-2024 behaviour — the URL is no longer attached to `getTasks` responses)
- Outer wrapper unzipped with the OS `unzip` binary, then inner `Part-N.zip` shards unwrapped in place
- Atomic staging → quality-gate → promote (a partial export can't overwrite a good backup)
- Static HTML rendered from markdown for offline browsing
- Playwright UI fallback + public-API fallback available for incident recovery

## Use This Template

This repository is designed to be copied into your own private GitHub repo. When used as a GitHub template, the workflow and exporter code are copied, but your GitHub Secrets and backup data are not.

Recommended setup:

1. Create a new **private** repo from this template.
2. Add the required repository secrets (see below): `NATIVE_EXPORT_TOKEN_V2`, `NATIVE_EXPORT_FILE_TOKEN`, `NOTION_SPACE_ID`.
3. Run the `Notion Backup` workflow manually once from the Actions tab.
4. Keep `main` for code and config.
5. Let the workflow publish generated backups to the `notion-backups` branch.

The workflow is intentionally included in the template. If it runs before secrets are configured, it exits successfully with a setup notice and does not create backup files or branches.

## Privacy and Security

Exported backups contain the full content of your Notion workspace — pages, databases, attachments, comments. Treat the backup branch and any local `backups/` folder as sensitive data.

- Keep repos containing real backups **private**.
- Do not commit `.env`, cookie values, `token_v2`, or `file_token`.
- Do not make a repo public if its Git history ever contained real backups.
- For public sharing, create a fresh repo with clean history and no generated backup files.

## Branch model (load-bearing)

- **`main`** — workflow file, scripts, config only. `backups/` is gitignored on `main`; never commit backup output here.
- **`notion-backups`** — orphan branch dedicated to backup artifacts. Each successful workflow run replaces the full `backups/` tree with a fresh snapshot and pushes a new commit. History exists, but each commit is a full snapshot, not a diff.

Browse a snapshot:

```bash
git switch notion-backups          # check out the latest snapshot
git log notion-backups --oneline   # see all daily snapshots
```

The workflow restores the previous snapshot from `notion-backups` into the runner before exporting, so the exporter can do incremental work and the quality gate can compare against prior counts.

## Repository layout (on `main`)

- `scripts/export_official_zip.mjs` — primary daily export (Node, ESM). Talks to Notion's internal `enqueueTask` / `getTasks` / `getNotificationLogV2` endpoints.
- `scripts/export_workspace_ui.mjs` — Playwright fallback that drives a real Chromium session if the API path stalls.
- `scripts/export_official.py` — public-API fallback (Markdown reconstructed from the REST API). Manual-only, lower fidelity.
- `scripts/render_html.py` — Markdown → static HTML for `backups/site/`.
- `scripts/export_native_fallback.mjs` — older root-scoped fallback. Not wired into the workflow.
- `.github/workflows/notion-backup.yml` — scheduler, restore-from-backup-branch, export, publish-to-backup-branch.
- `backup.config.yaml` — committed default tunables (rate limits, quality gates, retention).
- `.env.example` — enumerates supported env keys for local runs.
- `backups/` — gitignored on `main`; only exists at runtime as a staging area before publish.

## Required GitHub Secrets

| Secret | Purpose |
|---|---|
| `NATIVE_EXPORT_TOKEN_V2` | Notion `token_v2` cookie (required) |
| `NATIVE_EXPORT_FILE_TOKEN` | Notion `file_token` cookie (optional but recommended) |
| `NOTION_SPACE_ID` | Notion workspace ID as dashed UUID, e.g. `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` |
| `NOTION_API_KEY` | Public-API integration token, **only** needed for the manual REST-API fallback |

To add a secret: open your GitHub repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.

## Cookie retrieval (and rotation)

The cookies are how the script authenticates as you. They expire when your Notion session ends — when that happens the workflow fails at the `enqueueTask` step with an auth error and you need to refresh them.

1. Open https://www.notion.so in your browser and log in.
2. DevTools → Application → Storage → Cookies → `https://www.notion.so`.
3. Copy the `token_v2` value → paste into the GitHub Secret `NATIVE_EXPORT_TOKEN_V2`.
4. Copy the `file_token` value → paste into the GitHub Secret `NATIVE_EXPORT_FILE_TOKEN`.
5. Update the same values in your local `.env` if you also run the script locally.

Cookies typically last weeks to months. There is no API-token alternative — Notion does not expose a public endpoint for full-workspace export.

## Finding your workspace ID (`NOTION_SPACE_ID`)

Open any page in your Notion workspace in the browser and look at the URL — the workspace ID is embedded in the page's metadata. The easiest way:

1. In your logged-in Notion tab, open DevTools → Console.
2. Paste: `document.cookie` and look for context, or run any in-app action and inspect a Network request — the request payload contains a `spaceId` UUID.
3. Copy the dashed UUID (e.g. `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`) into the GitHub Secret `NOTION_SPACE_ID`.

Must be the **dashed** UUID form, not the 32-char compact form.

## Export modes

- **`workspace`** (default) — one full workspace export per run, driven by `NOTION_SPACE_ID`.
- **`roots`** (legacy) — per-root-page exports, driven by `OFFICIAL_EXPORT_ROOT_PAGE_IDS` env or `backups/manifests/roots.json`. Kept for emergency recovery.

Workspace-mode tunables (env vars or `backup.config.yaml`):

| Env | Default | Purpose |
|---|---|---|
| `OFFICIAL_WORKSPACE_RETRYABLE_FAILURE_RETRIES` | `1` | How many fresh task attempts to make if Notion returns `retryable_failure` |
| `OFFICIAL_WORKSPACE_POLL_TIMEOUT_SECONDS` | `10800` (3 hr) | Per-attempt budget for Notion's server-side export to complete |
| `OFFICIAL_WORKSPACE_UI_FALLBACK` | `1` | If `1`, fall back to Playwright UI download when API path fails |
| `OFFICIAL_WORKSPACE_UI_TIMEOUT_SECONDS` | `2400` (40 min) | Playwright fallback budget |

For reference, real exports of a ~3,300-page workspace take ~12 minutes end-to-end on the runner (most of which is Notion's server-side work).

## Schedule

Defined in `.github/workflows/notion-backup.yml`:

```yaml
schedule:
  - cron: "0 2 * * *"   # daily at 02:00 UTC
  - cron: "0 3 * * 0"   # extra Sunday run at 03:00 UTC
```

Plus `workflow_dispatch` for manual runs.

GitHub disables scheduled workflows on repos with no activity for ~60 days. Daily snapshots landing on `notion-backups` keep the schedule alive automatically.

## Local run

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
npm install
npx playwright install chromium

cp .env.example .env
# fill in NATIVE_EXPORT_TOKEN_V2, NATIVE_EXPORT_FILE_TOKEN, NOTION_SPACE_ID

node scripts/export_official_zip.mjs   # primary export
python scripts/render_html.py          # markdown -> HTML

# optional manual API fallback
python scripts/export_official.py
```

Requires the `unzip` binary on `PATH` (default on macOS, default on GitHub Actions `ubuntu-latest`).

## How a run works end-to-end

1. Workflow starts (cron or manual).
2. Restore previous `backups/` from the `notion-backups` branch into the runner workspace.
3. `export_official_zip.mjs`:
   - `POST /api/v3/enqueueTask` with `exportSpace` and your space ID → get back `taskId`.
   - Poll `POST /api/v3/getTasks` *and* `POST /api/v3/getNotificationLogV2` in parallel. The export URL arrives via the notifications endpoint (an `export-completed` activity with `start_time >= taskStartedAt`).
   - Download the signed ZIP from the URL.
   - `unzip` the outer wrapper into `backups/.staging/<run>/backups/official/`.
   - Find any `*-Part-N.zip` inner archives and `unzip` each in place (Notion delivers exports as wrapper + parts via the API path).
4. Recursive scan counts `.md`, `.csv`, `.html` files. Quality gate compares to the prior snapshot.
5. If the quality gate passes, promote staging → `backups/official/` atomically.
6. `render_html.py` builds `backups/site/`.
7. Publish step: switch to `notion-backups` branch, wipe worktree, copy fresh `backups/` in, commit, push.

## Quality gates

Block the promote if any of these fail (env vars override defaults):

- `QUALITY_MIN_MD_COUNT` (default `100`) — minimum Markdown files
- `QUALITY_MIN_CSV_COUNT` (default `10`) — minimum CSV files
- `QUALITY_MAX_DROP_PERCENT` (default `60`) — max allowed drop vs. previous run

A failed gate means `backups/official/` on `notion-backups` keeps its previous content — partial/empty exports cannot destroy good history. Tune the minimums to match the size of your workspace; the defaults are calibrated for a workspace with at least ~100 pages.

## Run reports

Written to `backups/manifests/` and uploaded as workflow artifacts:

- `run_summary.json` — counts, timings, retry totals, success/failure verdict
- `failures.json` — per-target failures with stage + error details
- `coverage_report.json` — quality-gate accounting (current vs. previous counts)
- `workspace_export.json` — workspace-mode metadata (poll timeouts, fallback strategy chosen)

## Limits and gotchas

- **Cookies expire.** When the workflow starts failing at `enqueueTask` with auth errors, rotate `NATIVE_EXPORT_TOKEN_V2` (and `FILE_TOKEN`) per the steps above.
- **Notion's export is not a workspace-restoration format.** It's a high-fidelity snapshot of content — readable, grep-able, diff-able — but does not preserve database views, relations, rollups, or formulas in a way that round-trips through Notion's import. Per Notion's own docs: *"You can't instantly recreate your workspace by reuploading your exported workspace content."* For 95% of "I need to read a lost page" scenarios this is fine.
- **Notion's API is undocumented and internal.** The endpoints used (`enqueueTask`, `getTasks`, `getNotificationLogV2`) are the same ones Notion's web UI uses. They can change.
- **The runner needs `unzip`.** GitHub `ubuntu-latest` has it. macOS has it. If you ever switch to a different image, verify `which unzip` returns a path.
- **First run on a fresh template** will exit cleanly with a setup notice until you add the required secrets. After secrets are configured, run the workflow manually from the Actions tab to seed the `notion-backups` branch — subsequent scheduled runs build on that.

## License

MIT — see [LICENSE](./LICENSE).

#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import { spawnSync } from "node:child_process";
import dotenv from "dotenv";

let retryAttemptsTotal = 0;

function nowIso() {
  return new Date().toISOString();
}

function runId() {
  return nowIso().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z");
}

function parseBoolLike(value, defaultValue = false) {
  if (value === undefined || value === null || value === "") return defaultValue;
  if (typeof value === "boolean") return value;
  const s = String(value).trim().toLowerCase();
  if (["1", "true", "yes", "on"].includes(s)) return true;
  if (["0", "false", "no", "off"].includes(s)) return false;
  return defaultValue;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function parseRetryAfter(value) {
  if (!value) return null;
  const n = Number(value);
  if (Number.isFinite(n) && n > 0) return Math.floor(n * 1000);
  const ts = Date.parse(value);
  if (!Number.isNaN(ts)) {
    const delta = ts - Date.now();
    if (delta > 0) return delta;
  }
  return null;
}

function stageLabel(pathname) {
  return pathname === "enqueueTask" ? "enqueue_task" : "poll_task";
}

function progressBar(done, total, width = 38) {
  const safeTotal = Math.max(1, total);
  const ratio = Math.max(0, Math.min(1, done / safeTotal));
  const filled = Math.floor(ratio * width);
  const head = filled < width ? "╸" : "";
  return `${"━".repeat(filled)}${head}${"━".repeat(Math.max(0, width - filled - (head ? 1 : 0)))}`;
}

function formatEta(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "--";
  const s = Math.floor(seconds);
  const mins = Math.floor(s / 60);
  const secs = s % 60;
  const hours = Math.floor(mins / 60);
  const remMins = mins % 60;
  if (hours > 0) return `${hours}h${String(remMins).padStart(2, "0")}m`;
  if (mins > 0) return `${mins}m${String(secs).padStart(2, "0")}s`;
  return `${secs}s`;
}

function loadWorkspaceRootMeta(repoRoot) {
  const manifestPath = path.join(repoRoot, "backups", "manifests", "manifest.json");
  const manifest = readJsonIfExists(manifestPath, null);
  const map = {};
  const entries = Array.isArray(manifest?.entries) ? manifest.entries : [];
  for (const e of entries) {
    if (e?.object === "page" && e?.parent_type === "workspace" && e?.id) {
      map[normalizeId(e.id)] = e?.title || normalizeId(e.id).slice(0, 8);
    }
  }
  return map;
}

function mkdirp(dir) {
  fs.mkdirSync(dir, { recursive: true });
}

function readJsonIfExists(filePath, fallback = null) {
  try {
    if (!fs.existsSync(filePath)) return fallback;
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch {
    return fallback;
  }
}

function writeJson(filePath, value) {
  mkdirp(path.dirname(filePath));
  fs.writeFileSync(filePath, JSON.stringify(value, null, 2) + "\n");
}

function parseScalar(raw) {
  const value = raw.trim();
  if (value === "true") return true;
  if (value === "false") return false;
  if (value === "[]") return [];
  if (/^-?\d+(\.\d+)?$/.test(value)) return Number(value);
  if (value.startsWith("[") && value.endsWith("]")) {
    return value
      .slice(1, -1)
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
  }
  return value.replace(/^["']|["']$/g, "");
}

function readBackupConfig(repoRoot) {
  const filePath = path.join(repoRoot, "backup.config.yaml");
  if (!fs.existsSync(filePath)) return {};
  const lines = fs.readFileSync(filePath, "utf8").split(/\r?\n/);
  const out = {};
  let section = null;
  for (const line of lines) {
    if (!line.trim() || line.trim().startsWith("#")) continue;
    if (!line.startsWith("  ")) {
      const m = line.match(/^([A-Za-z0-9_-]+):\s*(.*)$/);
      if (!m) continue;
      const [, k, v] = m;
      if (v === "") {
        section = k;
        if (!out[section]) out[section] = {};
      } else {
        out[k] = parseScalar(v);
        section = null;
      }
      continue;
    }
    if (!section) continue;
    const m = line.match(/^\s{2}([A-Za-z0-9_-]+):\s*(.*)$/);
    if (!m) continue;
    const [, k, v] = m;
    out[section][k] = parseScalar(v);
  }
  return out;
}

function sha256File(filePath) {
  const hash = crypto.createHash("sha256");
  hash.update(fs.readFileSync(filePath));
  return hash.digest("hex");
}

function listFilesRecursive(dir, predicate = () => true) {
  if (!fs.existsSync(dir)) return [];
  const out = [];
  const stack = [dir];
  while (stack.length > 0) {
    const cur = stack.pop();
    const items = fs.readdirSync(cur, { withFileTypes: true });
    for (const item of items) {
      const full = path.join(cur, item.name);
      if (item.isDirectory()) {
        stack.push(full);
      } else if (item.isFile() && predicate(full)) {
        out.push(full);
      }
    }
  }
  return out;
}

function readRootPageIds(repoRoot, cfg) {
  const envRoots = (process.env.OFFICIAL_EXPORT_ROOT_PAGE_IDS || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  if (envRoots.length > 0) return envRoots;

  const cfgRoots = Array.isArray(cfg?.official_zip?.root_page_ids) ? cfg.official_zip.root_page_ids : [];
  if (cfgRoots.length > 0) return cfgRoots;

  const rootsPath = path.join(repoRoot, "backups", "manifests", "roots.json");
  const roots = readJsonIfExists(rootsPath, {});
  const ids = Array.isArray(roots?.root_page_ids) ? roots.root_page_ids : [];
  return ids.filter((v) => typeof v === "string" && v.length > 0);
}

function readOfficialZipMode(cfg) {
  const fromEnv = String(process.env.OFFICIAL_EXPORT_MODE || "").trim().toLowerCase();
  if (fromEnv === "workspace" || fromEnv === "roots") return fromEnv;
  const fromCfg = String(cfg?.official_zip?.mode || "").trim().toLowerCase();
  if (fromCfg === "workspace" || fromCfg === "roots") return fromCfg;
  return "workspace";
}

function normalizeId(id) {
  return String(id || "").replace(/-/g, "").toLowerCase();
}

function toDashedUuid(id) {
  const clean = normalizeId(id);
  if (!/^[0-9a-f]{32}$/.test(clean)) return null;
  return `${clean.slice(0, 8)}-${clean.slice(8, 12)}-${clean.slice(12, 16)}-${clean.slice(16, 20)}-${clean.slice(20)}`;
}

function parseSpaceId(value) {
  const raw = String(value || "").trim();
  const dashed = toDashedUuid(raw);
  if (!dashed) return null;
  return {
    raw,
    dashed,
    canonical: normalizeId(raw),
    format: raw.includes("-") ? "dashed" : "compact",
  };
}

function blockIdFromInput(value) {
  const raw = String(value || "").trim();
  const dashed = toDashedUuid(raw);
  if (dashed) return dashed;
  try {
    const u = new URL(raw);
    if (!u.hostname.endsWith("notion.so") && !u.hostname.endsWith("notion.site")) return null;
    const parts = u.pathname.slice(1).split("-");
    return toDashedUuid(parts[parts.length - 1]);
  } catch {
    return null;
  }
}

function notionCookieHeader(tokenV2, fileToken) {
  return `token_v2=${tokenV2};file_token=${fileToken}`;
}

async function notionApiPost(pathname, body, cookieHeader) {
  const maxAttempts = 16;
  const stage = stageLabel(pathname);
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    let res;
    try {
      res = await fetch(`https://www.notion.so/api/v3/${pathname}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Cookie: cookieHeader,
        },
        body: JSON.stringify(body),
      });
    } catch (err) {
      if (attempt < maxAttempts) {
        retryAttemptsTotal += 1;
        await sleep(350 * attempt);
        continue;
      }
      throw Object.assign(new Error(`Notion API ${pathname} fetch failed: ${String(err?.message || err)}`), {
        stage,
      });
    }

    if (res.ok) {
      return res.json();
    }

    const status = res.status;
    if ((status === 429 || status >= 500) && attempt < maxAttempts) {
      retryAttemptsTotal += 1;
      const retryAfterMs = parseRetryAfter(res.headers.get("retry-after"));
      const delay = retryAfterMs ?? Math.min(30000, 1500 * attempt);
      await sleep(delay);
      continue;
    }

    throw Object.assign(new Error(`Notion API ${pathname} failed: HTTP ${status}`), {
      stage,
      httpStatus: status,
    });
  }
  throw Object.assign(new Error(`Notion API ${pathname} failed after retries`), {
    stage,
  });
}

async function downloadExportZip(exportUrl, cookieHeader, maxAttempts = 3) {
  let exportUrlHost = null;
  try {
    exportUrlHost = new URL(exportUrl).host;
  } catch {
    exportUrlHost = null;
  }
  let lastErr = null;
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    try {
      const res = await fetch(exportUrl, {
        method: "GET",
        headers: {
          Cookie: cookieHeader,
          Referer: "https://www.notion.so/",
          Origin: "https://www.notion.so",
          "User-Agent": "Mozilla/5.0 (NotionBackupExporter)",
        },
      });
      if (!res.ok) {
        const body = await res.text().catch(() => "");
        const snippet = body ? body.slice(0, 200).replace(/\s+/g, " ") : "";
        throw Object.assign(new Error(`export download failed: HTTP ${res.status}${snippet ? ` body=${snippet}` : ""}`), {
          httpStatus: res.status,
          stage: "download_zip",
          exportUrlHost,
        });
      }
      const arr = await res.arrayBuffer();
      return Buffer.from(arr);
    } catch (err) {
      lastErr = Object.assign(err instanceof Error ? err : new Error(String(err)), {
        stage: err?.stage || "download_zip",
        httpStatus: err?.httpStatus ?? null,
        exportUrlHost,
      });
      if (attempt < maxAttempts) {
        retryAttemptsTotal += 1;
        await sleep(400 * attempt);
        continue;
      }
    }
  }
  throw lastErr || new Error("export download failed");
}

function extractZipToDir(zipPath, destDir) {
  // Use the OS unzip binary instead of adm-zip. adm-zip silently skips entries
  // on Notion exports (long paths + UTF-8 filenames), producing 0 files with
  // no exception. unzip is present on GitHub Actions ubuntu-latest and macOS.
  mkdirp(destDir);
  const result = spawnSync("unzip", ["-q", "-o", zipPath, "-d", destDir], {
    encoding: "utf8",
    maxBuffer: 1024 * 1024 * 16,
  });
  if (result.status !== 0) {
    const stderr = (result.stderr || "").trim().slice(0, 500);
    const stdout = (result.stdout || "").trim().slice(0, 200);
    throw new Error(
      `unzip failed (exit ${result.status}): stderr="${stderr}" stdout="${stdout}"`
    );
  }
}

function extractInnerPartZips(dir) {
  // Notion's API-delivered workspace exports come as a wrapper ZIP that, when
  // unzipped, contains one or more "Export-<uuid>-Part-N.zip" inner archives.
  // Each inner zip holds the real .md/.csv/.html files. Unwrap them in place
  // and remove the consumed Part-zips so the staging tree is clean.
  let extracted = 0;
  try {
    const entries = fs.readdirSync(dir);
    for (const name of entries) {
      if (!/-Part-\d+\.zip$/i.test(name)) continue;
      const partPath = path.join(dir, name);
      extractZipToDir(partPath, dir);
      fs.unlinkSync(partPath);
      extracted += 1;
    }
  } catch (err) {
    process.stdout.write(`\n[extract-inner] error: ${String(err?.message || err)}\n`);
    throw err;
  }
  if (extracted > 0) {
    process.stdout.write(`\n[extract-inner] unwrapped ${extracted} Part-N.zip file(s) in ${dir}\n`);
  }
}

function logZipDiagnostic(zipPath, label) {
  // Read-only probe: print size + first 16 bytes (hex + printable ASCII) of a
  // downloaded "zip" so we can tell if it's a real ZIP (PK magic) or an HTML
  // error page / login redirect / empty stub.
  try {
    const stat = fs.statSync(zipPath);
    const fd = fs.openSync(zipPath, "r");
    const buf = Buffer.alloc(16);
    fs.readSync(fd, buf, 0, 16, 0);
    fs.closeSync(fd);
    const hex = buf.toString("hex");
    const ascii = buf.toString("utf8").replace(/[^\x20-\x7e]/g, ".");
    const isZipMagic = hex.startsWith("504b0304") || hex.startsWith("504b0506") || hex.startsWith("504b0708");
    process.stdout.write(
      `\n[zip-diag] ${label} bytes=${stat.size} hex16=${hex} ascii16="${ascii}" zip_magic=${isZipMagic}\n`
    );
  } catch (err) {
    process.stdout.write(`\n[zip-diag] ${label} could not stat/read ${zipPath}: ${String(err?.message || err)}\n`);
  }
}

async function fetchExportUrlFromNotifications(spaceIdDashed, startTimeMs, cookieHeader) {
  // Since ~mid-2024 Notion no longer attaches the export URL to getTasks responses.
  // The URL is delivered via the notifications panel instead. Mirrors PR #44 on
  // darobin/notion-backup. Returns the first export-completed link with start_time >= startTimeMs.
  try {
    const data = await notionApiPost(
      "getNotificationLogV2",
      { spaceId: spaceIdDashed, size: 20, type: "unread_and_read", variant: "no_grouping" },
      cookieHeader
    );
    const activities = Object.values(data?.recordMap?.activity || {});
    for (const activity of activities) {
      const v = activity?.value?.value;
      if (!v) continue;
      if (v.type !== "export-completed") continue;
      const ts = Number(v.start_time);
      if (!Number.isFinite(ts) || ts < startTimeMs) continue;
      const link = v?.edits?.[0]?.link;
      if (typeof link === "string" && link.length > 0) {
        return link;
      }
    }
    return null;
  } catch {
    return null;
  }
}

async function getExportZip(rootId, tokenV2, fileToken) {
  const blockId = blockIdFromInput(rootId);
  if (!blockId) {
    throw new Error(`Invalid root page ID or URL: ${rootId}`);
  }
  const cookieHeader = notionCookieHeader(tokenV2, fileToken);
  const enqueue = await notionApiPost(
    "enqueueTask",
    {
      task: {
        eventName: "exportBlock",
        request: {
          block: { id: blockId },
          recursive: true,
          shouldExportComments: false,
          exportOptions: {
            exportType: "markdown",
            timeZone: "UTC",
            locale: "en",
            collectionViewExportType: "all",
          },
        },
      },
    },
    cookieHeader
  );
  const taskId = enqueue?.taskId;
  if (!taskId) {
    throw new Error(`enqueueTask returned no taskId for ${rootId}`);
  }

  const deadline = Date.now() + 10 * 60 * 1000;
  while (Date.now() < deadline) {
    await sleep(1000);
    const tasks = await notionApiPost("getTasks", { taskIds: [taskId] }, cookieHeader);
    const task = tasks?.results?.find((t) => t?.id === taskId) || tasks?.results?.[0];
    const state = task?.state;
    const exportUrl = task?.status?.exportURL;
    if (state === "success" && exportUrl) {
      return downloadExportZip(exportUrl, cookieHeader, 3);
    }
    if (state && !["in_progress", "not_started"].includes(state)) {
      throw Object.assign(new Error(`export task failed with state=${state}`), { stage: "poll_task" });
    }
  }
  throw Object.assign(new Error(`export task timed out after 10m`), { stage: "poll_task" });
}

async function getWorkspaceExportZip(spaceId, tokenV2, fileToken, options = {}) {
  const parsed = parseSpaceId(spaceId);
  if (!parsed) {
    throw new Error("NOTION_SPACE_ID must be a 32-char hex UUID (dashes allowed)");
  }
  const maxTaskAttempts = Number(options.maxTaskAttempts || 3);
  const pollTimeoutMs = Number(options.pollTimeoutMs || 25 * 60 * 1000);
  const cookieHeader = notionCookieHeader(tokenV2, fileToken);

  let lastErr = null;
  for (let taskAttempt = 1; taskAttempt <= maxTaskAttempts; taskAttempt += 1) {
    const taskStartedAt = Date.now();
    const enqueue = await notionApiPost(
      "enqueueTask",
      {
        task: {
          eventName: "exportSpace",
          request: {
            spaceId: parsed.dashed,
            shouldExportComments: false,
            exportOptions: {
              exportType: "markdown",
              timeZone: "UTC",
              locale: "en",
            },
          },
        },
      },
      cookieHeader
    );
    const taskId = enqueue?.taskId;
    if (!taskId) {
      throw new Error(`enqueueTask returned no taskId for space ${parsed.dashed}`);
    }

    const deadline = Date.now() + pollTimeoutMs;
    let lastNotifPollAt = 0;
    while (Date.now() < deadline) {
      await sleep(1200);
      const tasks = await notionApiPost("getTasks", { taskIds: [taskId] }, cookieHeader);
      const task = tasks?.results?.find((t) => t?.id === taskId) || tasks?.results?.[0];
      const state = task?.state;
      const exportUrl =
        task?.status?.exportURL ||
        task?.status?.exportUrl ||
        task?.status?.export_url ||
        task?.status?.url ||
        task?.status?.signedUrl ||
        null;
      if (state === "success" && exportUrl) {
        return downloadExportZip(exportUrl, cookieHeader, 3);
      }
      // Notion stopped attaching the URL to getTasks responses; the URL is now
      // delivered via the notifications panel. Throttle to one notif poll per 5s.
      if (Date.now() - lastNotifPollAt >= 5000) {
        lastNotifPollAt = Date.now();
        const notifUrl = await fetchExportUrlFromNotifications(parsed.dashed, taskStartedAt, cookieHeader);
        if (notifUrl) {
          return downloadExportZip(notifUrl, cookieHeader, 3);
        }
      }
      if (state === "success" && !exportUrl) {
        // Notion can mark success before the signed download URL is attached.
        continue;
      }
      if (!state || ["in_progress", "not_started"].includes(state)) {
        continue;
      }
      if (state === "retryable_failure" && taskAttempt < maxTaskAttempts) {
        retryAttemptsTotal += 1;
        await sleep(Math.min(15000, 2500 * taskAttempt));
        lastErr = Object.assign(new Error(`workspace export task failed with state=${state}`), {
          stage: "poll_task",
          taskState: state,
          taskId,
          taskAttempt,
          maxTaskAttempts,
        });
        break;
      }
      throw Object.assign(new Error(`workspace export task failed with state=${state}`), {
        stage: "poll_task",
        taskState: state,
        taskId,
        taskAttempt,
        maxTaskAttempts,
      });
    }
    if (Date.now() >= deadline) {
      lastErr = Object.assign(new Error(`workspace export task timed out after ${Math.round(pollTimeoutMs / 1000)}s`), {
        stage: "poll_task",
        taskState: "timeout",
        taskAttempt,
        maxTaskAttempts,
      });
    }
  }
  throw lastErr || Object.assign(new Error("workspace export task failed"), { stage: "poll_task" });
}

function promoteDir(src, dst) {
  if (fs.existsSync(dst)) fs.rmSync(dst, { recursive: true, force: true });
  fs.renameSync(src, dst);
}

function runWorkspaceUiFallback(repoRoot, spaceIdDashed, zipPath, timeoutSeconds) {
  const scriptPath = path.join(repoRoot, "scripts", "export_workspace_ui.mjs");
  const child = spawnSync(
    process.execPath,
    [scriptPath, "--space-id", spaceIdDashed, "--out", zipPath, "--timeout-seconds", String(timeoutSeconds)],
    {
      cwd: repoRoot,
      env: process.env,
      encoding: "utf8",
      maxBuffer: 1024 * 1024 * 4,
    }
  );
  if (child.status === 0) {
    return {
      ok: true,
      stdout: child.stdout || "",
      stderr: child.stderr || "",
    };
  }
  const msg =
    (child.stderr || "").trim() ||
    (child.stdout || "").trim() ||
    `UI fallback failed with exit code ${String(child.status)}`;
  throw Object.assign(new Error(msg), {
    stage: "ui_fallback",
    exitCode: child.status ?? null,
  });
}

function gateQuality(current, previous, minMdCount, minCsvCount, maxDropPercent) {
  const failures = [];
  if (current.md_count < minMdCount) {
    failures.push(`md_count ${current.md_count} < min_md_count ${minMdCount}`);
  }
  if (current.csv_count < minCsvCount) {
    failures.push(`csv_count ${current.csv_count} < min_csv_count ${minCsvCount}`);
  }

  if (previous && typeof previous.md_count === "number" && previous.md_count > 0) {
    const dropMd = ((previous.md_count - current.md_count) / previous.md_count) * 100;
    if (dropMd > maxDropPercent) {
      failures.push(
        `md_count drop ${dropMd.toFixed(2)}% exceeds max_drop_percent ${maxDropPercent}%`
      );
    }
  }

  if (previous && typeof previous.csv_count === "number" && previous.csv_count > 0) {
    const dropCsv = ((previous.csv_count - current.csv_count) / previous.csv_count) * 100;
    if (dropCsv > maxDropPercent) {
      failures.push(
        `csv_count drop ${dropCsv.toFixed(2)}% exceeds max_drop_percent ${maxDropPercent}%`
      );
    }
  }
  return failures;
}

async function main() {
  const repoRoot = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..");
  dotenv.config({ path: path.join(repoRoot, ".env"), override: false });
  const cfg = readBackupConfig(repoRoot);

  const tokenV2 = process.env.NATIVE_EXPORT_TOKEN_V2;
  const fileToken = process.env.NATIVE_EXPORT_FILE_TOKEN ?? "";
  if (!tokenV2) {
    console.error("NATIVE_EXPORT_TOKEN_V2 is required");
    return 2;
  }

  const exportMode = readOfficialZipMode(cfg);
  const rootTitleById = loadWorkspaceRootMeta(repoRoot);
  const configuredRoots = readRootPageIds(repoRoot, cfg).map(normalizeId);
  const manifestDiscoveredRoots = Object.keys(rootTitleById).map(normalizeId);
  const roots = (configuredRoots.length > 0 ? configuredRoots : manifestDiscoveredRoots).map(normalizeId);
  const rootItems = roots.map((id) => ({ id, title: rootTitleById[id] || id.slice(0, 8) }));
  const parsedSpace = parseSpaceId(process.env.NOTION_SPACE_ID || "");
  const spaceId = parsedSpace?.canonical || "";
  if (exportMode === "workspace" && !parsedSpace) {
    console.error("NOTION_SPACE_ID is required in workspace mode");
    return 2;
  }
  if (exportMode === "roots" && rootItems.length === 0) {
    console.error("No root page IDs found. Set OFFICIAL_EXPORT_ROOT_PAGE_IDS or backups/manifests/roots.json");
    return 2;
  }

  const minMdCount = Number(process.env.QUALITY_MIN_MD_COUNT || cfg?.quality?.min_md_count || "1");
  const minCsvCount = Number(process.env.QUALITY_MIN_CSV_COUNT || cfg?.quality?.min_csv_count || "0");
  const maxDropPercent = Number(
    process.env.QUALITY_MAX_DROP_PERCENT || cfg?.quality?.max_drop_percent || "60"
  );
  const keepZipDays = Number(
    process.env.OFFICIAL_ZIP_KEEP_DAYS || cfg?.official_zip?.keep_daily_zips_days || "30"
  );
  const workspaceRetryableRetries = Number(
    process.env.OFFICIAL_WORKSPACE_RETRYABLE_FAILURE_RETRIES ||
      cfg?.official_zip?.workspace_retryable_failure_retries ||
      "3"
  );
  const workspacePollTimeoutSeconds = Number(
    process.env.OFFICIAL_WORKSPACE_POLL_TIMEOUT_SECONDS ||
      cfg?.official_zip?.workspace_poll_timeout_seconds ||
      "1500"
  );
  const workspaceUiFallbackEnabled = parseBoolLike(
    process.env.OFFICIAL_WORKSPACE_UI_FALLBACK ?? cfg?.official_zip?.workspace_ui_fallback ?? true,
    true
  );
  const workspaceUiTimeoutSeconds = Number(
    process.env.OFFICIAL_WORKSPACE_UI_TIMEOUT_SECONDS || cfg?.official_zip?.workspace_ui_timeout_seconds || "2400"
  );

  const run = runId();
  const startedAt = nowIso();

  const canonicalBackups = path.join(repoRoot, "backups");
  const stagingRoot = path.join(canonicalBackups, ".staging", run, "backups");
  const stageOfficial = path.join(stagingRoot, "official");
  const stageManifests = path.join(stagingRoot, "manifests");
  const stageNativeDate = path.join(stagingRoot, "native", startedAt.slice(0, 10));
  mkdirp(stageOfficial);
  mkdirp(stageManifests);
  mkdirp(stageNativeDate);

  const previousCoverage = readJsonIfExists(
    path.join(canonicalBackups, "manifests", "coverage_report.json"),
    null
  );

  const failures = [];
  const zipRecords = [];
  let workspaceApiError = null;
  let workspaceUiFallbackUsed = false;
  let workspaceDownloadStrategy = exportMode === "workspace" ? "none" : null;
  const fileTokenMode = fileToken ? "provided" : "empty";
  if (!fileToken) {
    console.log("NATIVE_EXPORT_FILE_TOKEN is empty; proceeding with file_token=");
  }

  const spinnerFrames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
  let spinnerIndex = 0;
  const startedMs = Date.now();
  const totalTargets = exportMode === "workspace" ? 1 : rootItems.length;
  const renderProgress = (done) => {
    const frame = spinnerFrames[spinnerIndex % spinnerFrames.length];
    spinnerIndex += 1;
    const pct = totalTargets > 0 ? Math.floor((done / totalTargets) * 100) : 0;
    const bar = progressBar(done, totalTargets, 38);
    const elapsed = Math.max(0.001, (Date.now() - startedMs) / 1000);
    const rate = done / elapsed;
    const eta = rate > 0 ? (totalTargets - done) / rate : Infinity;
    const line = `${frame} Official export ${bar} ${String(pct).padStart(3)}% ${done}/${totalTargets} | ${rate.toFixed(2)}/s | ETA ${formatEta(eta)} | r:${retryAttemptsTotal} f:${failures.length}`;
    process.stdout.write(`\r${line}`);
  };

  renderProgress(0);
  const spinnerTimer = setInterval(() => renderProgress(zipRecords.length + failures.length), 120);

  if (exportMode === "workspace") {
    const zipPath = path.join(stageNativeDate, `workspace-${spaceId.slice(0, 8)}.zip`);
    try {
      const zipBuffer = await getWorkspaceExportZip(parsedSpace.dashed, tokenV2, fileToken, {
        maxTaskAttempts: workspaceRetryableRetries,
        pollTimeoutMs: workspacePollTimeoutSeconds * 1000,
      });
      fs.writeFileSync(zipPath, zipBuffer);
      logZipDiagnostic(zipPath, "api_path");
      extractZipToDir(zipPath, stageOfficial);
      extractInnerPartZips(stageOfficial);
      zipRecords.push({
        root_id: `workspace:${spaceId}`,
        zip_path: path
          .join("backups", "native", startedAt.slice(0, 10), `workspace-${spaceId.slice(0, 8)}.zip`)
          .replaceAll(path.sep, "/"),
        bytes: fs.statSync(zipPath).size,
        sha256: sha256File(zipPath),
      });
      workspaceDownloadStrategy = "api_export_url";
      process.stdout.write(`\nExported workspace ${spaceId.slice(0, 8)}\n`);
    } catch (err) {
      workspaceApiError = err;
      process.stdout.write(
        `\nWorkspace API path failed: ${String(err?.message || err)}${err?.taskState ? ` (task_state=${err.taskState})` : ""}\n`
      );
      if (workspaceUiFallbackEnabled) {
        process.stdout.write("Trying UI fallback download...\n");
        try {
          runWorkspaceUiFallback(repoRoot, parsedSpace.dashed, zipPath, workspaceUiTimeoutSeconds);
          logZipDiagnostic(zipPath, "ui_fallback");
          extractZipToDir(zipPath, stageOfficial);
          extractInnerPartZips(stageOfficial);
          zipRecords.push({
            root_id: `workspace:${spaceId}`,
            zip_path: path
              .join("backups", "native", startedAt.slice(0, 10), `workspace-${spaceId.slice(0, 8)}.zip`)
              .replaceAll(path.sep, "/"),
            bytes: fs.statSync(zipPath).size,
            sha256: sha256File(zipPath),
          });
          workspaceUiFallbackUsed = true;
          workspaceDownloadStrategy = "ui_download";
          process.stdout.write(`Exported workspace ${spaceId.slice(0, 8)} via UI fallback\n`);
        } catch (uiErr) {
          failures.push({
            kind: "official_zip_export",
            root_id: `workspace:${spaceId}`,
            root_title: "workspace",
            stage: uiErr?.stage || "ui_fallback",
            http_status: workspaceApiError?.httpStatus ?? null,
            export_url_host: workspaceApiError?.exportUrlHost ?? null,
            task_state: workspaceApiError?.taskState ?? null,
            task_id: workspaceApiError?.taskId ?? null,
            task_attempt: workspaceApiError?.taskAttempt ?? null,
            max_task_attempts: workspaceApiError?.maxTaskAttempts ?? null,
            space_id_format: parsedSpace?.format || null,
            file_token_mode: fileTokenMode,
            error_type: uiErr?.name || "Error",
            error: `api=${String(workspaceApiError?.message || workspaceApiError)} | ui=${String(
              uiErr?.message || uiErr
            )}`,
          });
          process.stdout.write(`Failed workspace ${spaceId.slice(0, 8)} with API+UI fallback\n`);
        }
      } else {
        failures.push({
          kind: "official_zip_export",
          root_id: `workspace:${spaceId}`,
          root_title: "workspace",
          stage: err?.stage || "unknown",
          http_status: err?.httpStatus ?? null,
          export_url_host: err?.exportUrlHost ?? null,
          task_state: err?.taskState ?? null,
          task_id: err?.taskId ?? null,
          task_attempt: err?.taskAttempt ?? null,
          max_task_attempts: err?.maxTaskAttempts ?? null,
          space_id_format: parsedSpace?.format || null,
          file_token_mode: fileTokenMode,
          error_type: err?.name || "Error",
          error: String(err?.message || err),
        });
        process.stdout.write(
          `\nFailed workspace ${spaceId.slice(0, 8)}: ${String(err?.message || err)}${err?.taskState ? ` (task_state=${err.taskState})` : ""}\n`
        );
      }
    }
  } else {
    for (const root of rootItems) {
      const rootId = root.id;
      const rootExportDir = path.join(stageOfficial, rootId);
      mkdirp(rootExportDir);
      const zipPath = path.join(stageNativeDate, `${rootId}.zip`);
      try {
        const zipBuffer = await getExportZip(rootId, tokenV2, fileToken);
        fs.writeFileSync(zipPath, zipBuffer);
        extractZipToDir(zipPath, rootExportDir);
        extractInnerPartZips(rootExportDir);
        zipRecords.push({
          root_id: rootId,
          zip_path: path.join("backups", "native", startedAt.slice(0, 10), `${rootId}.zip`).replaceAll(path.sep, "/"),
          bytes: fs.statSync(zipPath).size,
          sha256: sha256File(zipPath),
        });
        process.stdout.write(`\nExported root ${root.title} (${rootId.slice(0, 8)})\n`);
      } catch (err) {
        failures.push({
          kind: "official_zip_export",
          root_id: rootId,
          root_title: root.title,
          stage: err?.stage || "unknown",
          http_status: err?.httpStatus ?? null,
          export_url_host: err?.exportUrlHost ?? null,
          file_token_mode: fileTokenMode,
          error_type: err?.name || "Error",
          error: String(err?.message || err),
        });
        process.stdout.write(`\nFailed root ${root.title} (${rootId.slice(0, 8)}): ${String(err?.message || err)}\n`);
      }
    }
  }
  clearInterval(spinnerTimer);
  renderProgress(zipRecords.length + failures.length);
  process.stdout.write("\n");

  // Post-extraction diagnostic: tell us what actually landed on disk.
  const allFiles = listFilesRecursive(stageOfficial, () => true);
  const extCounts = {};
  for (const f of allFiles) {
    const ext = (f.match(/\.[^./\\]+$/) || ["(none)"])[0].toLowerCase();
    extCounts[ext] = (extCounts[ext] || 0) + 1;
  }
  const topLevel = (() => {
    try {
      return fs.readdirSync(stageOfficial).slice(0, 10);
    } catch {
      return [];
    }
  })();
  const sample = allFiles.slice(0, 8).map((p) => path.relative(stageOfficial, p));
  process.stdout.write(
    `\n[extract-diag] stage_official=${stageOfficial} total_files=${allFiles.length} ` +
      `top_level=${JSON.stringify(topLevel)} ` +
      `ext_counts=${JSON.stringify(extCounts)} ` +
      `sample=${JSON.stringify(sample)}\n`
  );

  const mdFiles = listFilesRecursive(stageOfficial, (f) => f.toLowerCase().endsWith(".md"));
  const csvFiles = listFilesRecursive(stageOfficial, (f) => f.toLowerCase().endsWith(".csv"));
  const htmlFiles = listFilesRecursive(stageOfficial, (f) => f.toLowerCase().endsWith(".html"));

  const currentCoverage = {
    generated_at: nowIso(),
    run_id: run,
    roots: exportMode === "workspace" ? [`workspace:${spaceId}`] : roots,
    md_count: mdFiles.length,
    csv_count: csvFiles.length,
    html_count: htmlFiles.length,
    zip_count: zipRecords.length,
    zip_records: zipRecords,
    previous: previousCoverage
      ? {
          run_id: previousCoverage.run_id || null,
          md_count: previousCoverage.md_count ?? null,
          csv_count: previousCoverage.csv_count ?? null,
          html_count: previousCoverage.html_count ?? null,
        }
      : null,
    quality: {
      min_md_count: minMdCount,
      min_csv_count: minCsvCount,
      max_drop_percent: maxDropPercent,
      failures: [],
    },
  };

  const qualityFailures = gateQuality(
    currentCoverage,
    previousCoverage,
    minMdCount,
    minCsvCount,
    maxDropPercent
  );
  currentCoverage.quality.failures = qualityFailures;

  if (failures.length > 0) {
    qualityFailures.push(exportMode === "workspace" ? "workspace_export_failed" : "one_or_more_root_exports_failed");
  }

  const runSummary = {
    run_id: run,
    started_at: startedAt,
    finished_at: nowIso(),
    primary_source: "official_zip",
    counts: {
      roots_total: totalTargets,
      roots_succeeded: zipRecords.length,
      roots_failed: failures.length,
      md_count: mdFiles.length,
      csv_count: csvFiles.length,
      html_count: htmlFiles.length,
    },
    quality_gate_passed: qualityFailures.length === 0,
    file_token_mode: fileTokenMode,
    export_mode: exportMode,
    workspace_id: exportMode === "workspace" ? spaceId : null,
    workspace_id_input_format: exportMode === "workspace" ? parsedSpace?.format || null : null,
    workspace_api: exportMode === "workspace"
      ? {
          retryable_failure_retries: workspaceRetryableRetries,
          poll_timeout_seconds: workspacePollTimeoutSeconds,
          ui_fallback_enabled: workspaceUiFallbackEnabled,
          ui_timeout_seconds: workspaceUiTimeoutSeconds,
        }
      : null,
    workspace_api_error: exportMode === "workspace" && workspaceApiError ? String(workspaceApiError?.message || workspaceApiError) : null,
    workspace_ui_fallback_used: exportMode === "workspace" ? workspaceUiFallbackUsed : null,
    workspace_download_strategy: workspaceDownloadStrategy,
  };

  writeJson(path.join(stageManifests, "coverage_report.json"), currentCoverage);
  writeJson(path.join(stageManifests, "run_summary.json"), runSummary);
  writeJson(path.join(stageManifests, "failures.json"), { generated_at: nowIso(), items: failures });
  const existingRootsManifest = readJsonIfExists(path.join(canonicalBackups, "manifests", "roots.json"), {});
  const existingRoots = Array.isArray(existingRootsManifest?.root_page_ids)
    ? existingRootsManifest.root_page_ids.map(normalizeId).filter(Boolean)
    : [];
  const rootsForManifest =
    exportMode === "workspace"
      ? (existingRoots.length > 0 ? existingRoots : manifestDiscoveredRoots)
      : roots;
  writeJson(path.join(stageManifests, "roots.json"), {
    generated_at: nowIso(),
    root_page_ids: rootsForManifest,
  });
  writeJson(path.join(stageManifests, "workspace_export.json"), {
    generated_at: nowIso(),
    export_mode: exportMode,
    workspace_id: exportMode === "workspace" ? spaceId : null,
    workspace_id_input_format: exportMode === "workspace" ? parsedSpace?.format || null : null,
    api_retryable_failure_retries: workspaceRetryableRetries,
    api_poll_timeout_seconds: workspacePollTimeoutSeconds,
    ui_fallback_enabled: workspaceUiFallbackEnabled,
    ui_timeout_seconds: workspaceUiTimeoutSeconds,
    api_error: workspaceApiError ? String(workspaceApiError?.message || workspaceApiError) : null,
    ui_fallback_used: workspaceUiFallbackUsed,
    download_strategy: workspaceDownloadStrategy,
  });
  writeJson(path.join(stageNativeDate, "metadata.json"), {
    generated_at: nowIso(),
    run_id: run,
    roots: exportMode === "workspace" ? [] : roots,
    export_mode: exportMode,
    workspace_id: exportMode === "workspace" ? spaceId : null,
    zips: zipRecords,
  });
  const sums = zipRecords.map((z) => `${z.sha256}  ${path.basename(z.zip_path)}`).join("\n");
  fs.writeFileSync(path.join(stageNativeDate, "SHA256SUMS"), sums ? `${sums}\n` : "");

  if (qualityFailures.length > 0) {
    console.error("Quality gate failed:");
    for (const line of qualityFailures) console.error(`- ${line}`);

    // Persist reports for CI artifact upload even on failure.
    const canonicalManifests = path.join(canonicalBackups, "manifests");
    mkdirp(canonicalManifests);
    for (const filename of [
      "coverage_report.json",
      "run_summary.json",
      "failures.json",
      "roots.json",
      "workspace_export.json",
    ]) {
      fs.copyFileSync(path.join(stageManifests, filename), path.join(canonicalManifests, filename));
    }
    console.log("\nRun summary:");
    console.log(JSON.stringify(runSummary, null, 2));
    if (failures.length > 0) {
      console.log("\nFailures:");
      console.log(JSON.stringify({ items: failures }, null, 2));
    }
    return 1;
  }

  mkdirp(canonicalBackups);
  promoteDir(stageOfficial, path.join(canonicalBackups, "official"));

  const canonicalNativeDate = path.join(canonicalBackups, "native", startedAt.slice(0, 10));
  mkdirp(path.dirname(canonicalNativeDate));
  promoteDir(stageNativeDate, canonicalNativeDate);

  // Keep native archives bounded by retention days.
  const nativeRoot = path.join(canonicalBackups, "native");
  if (fs.existsSync(nativeRoot) && keepZipDays > 0) {
    const dirs = fs
      .readdirSync(nativeRoot, { withFileTypes: true })
      .filter((e) => e.isDirectory())
      .map((e) => e.name)
      .sort();
    if (dirs.length > keepZipDays) {
      for (const old of dirs.slice(0, dirs.length - keepZipDays)) {
        fs.rmSync(path.join(nativeRoot, old), { recursive: true, force: true });
      }
    }
  }

  const canonicalManifests = path.join(canonicalBackups, "manifests");
  mkdirp(canonicalManifests);
  for (const filename of [
    "coverage_report.json",
    "run_summary.json",
    "failures.json",
    "roots.json",
    "workspace_export.json",
  ]) {
    fs.copyFileSync(path.join(stageManifests, filename), path.join(canonicalManifests, filename));
  }

  console.log(
    `Official export complete. mode=${runSummary.export_mode} roots_ok=${zipRecords.length} md=${mdFiles.length} csv=${csvFiles.length} quality=pass`
  );
  console.log("\nRun summary:");
  console.log(JSON.stringify(runSummary, null, 2));
  if (failures.length > 0) {
    console.log("\nFailures:");
    console.log(JSON.stringify({ items: failures }, null, 2));
  }
  return 0;
}

main()
  .then((code) => process.exit(code))
  .catch((err) => {
    console.error(err);
    process.exit(1);
  });

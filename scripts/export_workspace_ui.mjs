#!/usr/bin/env node
//
// DORMANT as of 2026-08-10 (official_zip.workspace_ui_fallback: false).
//
// This script has never produced a successful export in any workflow run, and
// it is currently broken end-to-end. Notion migrated the app off notion.so:
// www.notion.so/settings now redirects to app.notion.com, so the cookies this
// script installs on `.notion.so` do not apply and the session lands on the
// login page. The 120s wait for the export button then times out, which is
// what buried the real (unrelated) API error during the Aug 2026 outage.
//
// Verified findings for whoever revives it:
//   - Navigate app.notion.com, not www.notion.so.
//   - Cookies must be installed on the `.notion.com` domain.
//   - token_v2 alone is NOT enough: the client also needs a `notion_users`
//     cookie (URL-encoded JSON array of user ids) or it redirects to /login.
//     Use the id of the user that actually holds NOTION_SPACE_ID -- picking a
//     co-signed-in account with no spaces lands on the onboarding screen.
//   - The settings selectors below are unverified against the current UI.
//
import fs from "node:fs";
import path from "node:path";
import { chromium } from "playwright";

function arg(name, fallback = null) {
  const idx = process.argv.indexOf(name);
  if (idx < 0 || idx + 1 >= process.argv.length) return fallback;
  return process.argv[idx + 1];
}

function normalizeId(id) {
  return String(id || "").replace(/-/g, "").toLowerCase();
}

function toDashedUuid(id) {
  const clean = normalizeId(id);
  if (!/^[0-9a-f]{32}$/.test(clean)) return null;
  return `${clean.slice(0, 8)}-${clean.slice(8, 12)}-${clean.slice(12, 16)}-${clean.slice(16, 20)}-${clean.slice(20)}`;
}

function ensureDir(filePath) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
}

async function main() {
  const tokenV2 = String(process.env.NATIVE_EXPORT_TOKEN_V2 || "").trim();
  const fileToken = String(process.env.NATIVE_EXPORT_FILE_TOKEN || "").trim();
  if (!tokenV2) {
    throw new Error("NATIVE_EXPORT_TOKEN_V2 is required for UI fallback");
  }

  const spaceIdArg = arg("--space-id");
  const outPath = arg("--out");
  const timeoutSeconds = Number(arg("--timeout-seconds", "2400"));
  if (!spaceIdArg || !outPath) {
    throw new Error("Usage: node scripts/export_workspace_ui.mjs --space-id <id> --out <zip-path> [--timeout-seconds <n>]");
  }
  const spaceId = toDashedUuid(spaceIdArg);
  if (!spaceId) {
    throw new Error("Invalid --space-id");
  }

  ensureDir(outPath);
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ acceptDownloads: true });
  const cookies = [
    {
      name: "token_v2",
      value: tokenV2,
      domain: ".notion.so",
      path: "/",
      httpOnly: true,
      secure: true,
    },
  ];
  if (fileToken) {
    cookies.push({
      name: "file_token",
      value: fileToken,
      domain: ".notion.so",
      path: "/",
      httpOnly: true,
      secure: true,
    });
  }
  await context.addCookies(cookies);

  const page = await context.newPage();
  const timeoutMs = timeoutSeconds * 1000;
  await page.goto("https://www.notion.so/settings", { waitUntil: "domcontentloaded", timeout: 90000 });

  const exportAll = page.getByRole("button", { name: /export all workspace content/i });
  // Honor the caller's budget: this used to be hard-coded to 120s, which made
  // --timeout-seconds (and OFFICIAL_WORKSPACE_UI_TIMEOUT_SECONDS) meaningless.
  await exportAll.first().waitFor({ timeout: timeoutMs });
  await exportAll.first().click();

  // Configure markdown+csv when available.
  const markdownOption = page.getByRole("option", { name: /markdown/i });
  if (await markdownOption.first().isVisible().catch(() => false)) {
    await markdownOption.first().click().catch(() => {});
  }

  const triggerExport = page.getByRole("button", { name: /^export$/i });
  if (await triggerExport.first().isVisible().catch(() => false)) {
    await triggerExport.first().click();
  }

  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const downloadBtn = page.getByRole("button", { name: /download/i }).first();
    const visible = await downloadBtn.isVisible().catch(() => false);
    if (visible) {
      const [download] = await Promise.all([page.waitForEvent("download", { timeout: 120000 }), downloadBtn.click()]);
      await download.saveAs(outPath);
      await browser.close();
      console.log(JSON.stringify({ ok: true, strategy: "ui_download", out_path: outPath }));
      return;
    }
    await page.waitForTimeout(2000);
  }

  await browser.close();
  throw new Error("UI fallback timed out waiting for workspace export download");
}

main().catch((err) => {
  console.error(String(err?.message || err));
  process.exit(1);
});

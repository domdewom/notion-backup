#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import { execSync } from "node:child_process";
import dotenv from "dotenv";
import NotionExporter from "notion-exporter";

function sha256(filePath) {
  const hash = crypto.createHash("sha256");
  hash.update(fs.readFileSync(filePath));
  return hash.digest("hex");
}

const repoRoot = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..");
dotenv.config({ path: path.join(repoRoot, ".env"), override: false });
const rootsPath = path.join(repoRoot, "backups", "manifests", "roots.json");
if (!fs.existsSync(rootsPath)) {
  console.log("roots.json not found, skipping native fallback export");
  process.exit(0);
}

const tokenV2 = process.env.NATIVE_EXPORT_TOKEN_V2;
const fileToken = process.env.NATIVE_EXPORT_FILE_TOKEN;
if (!tokenV2 || !fileToken) {
  console.log("Native export secrets missing, skipping native fallback export");
  process.exit(0);
}

const roots = JSON.parse(fs.readFileSync(rootsPath, "utf8"));
const rootPageIds = Array.isArray(roots.root_page_ids) ? roots.root_page_ids : [];
if (rootPageIds.length === 0) {
  console.log("No root page ids found, skipping native fallback export");
  process.exit(0);
}

const today = new Date().toISOString().slice(0, 10);
const runDir = path.join(repoRoot, "backups", "native", today);
const exportsDir = path.join(runDir, "exports");
fs.mkdirSync(exportsDir, { recursive: true });

const exporter = new NotionExporter(tokenV2, fileToken);

for (const pageId of rootPageIds) {
  const outDir = path.join(exportsDir, pageId);
  fs.mkdirSync(outDir, { recursive: true });
  console.log(`Native export for root page ${pageId}`);
  await exporter.getMdFiles(pageId, outDir);
}

const metadata = {
  generated_at: new Date().toISOString(),
  root_page_ids: rootPageIds,
};
const metadataPath = path.join(runDir, "metadata.json");
fs.writeFileSync(metadataPath, JSON.stringify(metadata, null, 2) + "\n");

const zipPath = path.join(runDir, "native-export.zip");
execSync(`cd ${JSON.stringify(runDir)} && zip -rq native-export.zip exports metadata.json`);

const sums = `native-export.zip  ${sha256(zipPath)}\n`;
fs.writeFileSync(path.join(runDir, "SHA256SUMS"), sums);

console.log(`Native fallback export complete: ${zipPath}`);

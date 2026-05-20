#!/usr/bin/env python3
import csv
import datetime as dt
import gzip
import hashlib
import json
import os
import random
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from requests.exceptions import ChunkedEncodingError, ConnectTimeout, ConnectionError, ReadTimeout, Timeout

try:
    import yaml
except Exception:
    yaml = None

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

try:
    from rich.console import Console
    from rich.progress import (
        BarColumn,
        Progress,
        SpinnerColumn,
        TaskProgressColumn,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )
except Exception:
    Console = None
    Progress = None

API_BASE = "https://api.notion.com/v1"
DEFAULT_NOTION_VERSION = "2025-09-03"

UUID_32_RE = re.compile(r"([0-9a-fA-F]{32})")
UUID_DASHED_RE = re.compile(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")

MARKDOWN_LINK_RE = re.compile(r"\[(?P<label>[^\]]+)\]\((?P<url>https?://[^)]+)\)")
MENTION_PAGE_PAIR_RE = re.compile(r"<mention-page(?P<attrs>[^>]*)>(?P<label>.*?)</mention-page>", re.IGNORECASE | re.DOTALL)
MENTION_PAGE_SELF_RE = re.compile(r"<mention-page(?P<attrs>[^>]*)/?>", re.IGNORECASE)
DATABASE_TAG_RE = re.compile(r"<database(?P<attrs>[^>]*)>(?P<label>.*?)</database>", re.IGNORECASE | re.DOTALL)
GENERIC_URL_TAG_RE = re.compile(
    r"<(?P<tag>[a-zA-Z0-9_-]+)(?P<attrs>[^>]*)url=\"(?P<url>https?://[^\"]+)\"(?P<tail>[^>]*)/?>",
    re.IGNORECASE,
)


@dataclass
class BlockJsonConfig:
    enabled: bool = True
    frequency: str = "weekly"
    compression: str = "gzip"
    retention_count: int = 12


@dataclass
class Config:
    max_rps: float = 2.0
    retries: int = 6
    retry_backoff_seconds: list[int] = field(default_factory=lambda: [2, 5, 15, 30, 60])
    exclude_patterns: list[str] | None = None
    slug_strategy: str = "title_plus_id"
    block_json: BlockJsonConfig = field(default_factory=BlockJsonConfig)
    request_timeout_connect_seconds: int = 10
    request_timeout_read_seconds: int = 180
    failure_mode: str = "best_effort"
    failure_threshold_percent: float = 2.0
    max_consecutive_failures: int = 20
    staging_keep: int = 3
    checkpoint_enabled: bool = True
    checkpoint_flush_every: int = 25


@dataclass
class PhaseState:
    name: str
    total: int
    current: int = 0
    started_at: float = field(default_factory=time.time)
    failures: int = 0
    retries_total: int = 0


class BaseProgressReporter:
    def __init__(self, log_every: int = 25):
        self.log_every = max(1, log_every)
        self.active: PhaseState | None = None

    def start_phase(self, name: str, total: int):
        self.active = PhaseState(name=name, total=max(1, total))
        self._on_start(self.active)

    def advance(self, step: int = 1, retries_total: int | None = None, failures: int | None = None):
        if not self.active:
            return
        self.active.current = min(self.active.total, self.active.current + step)
        if retries_total is not None:
            self.active.retries_total = retries_total
        if failures is not None:
            self.active.failures = failures
        self._on_advance(self.active)

    def note(self, message: str):
        self._on_note(message)

    def finish_phase(self, summary: str = ""):
        if not self.active:
            return
        self.active.current = self.active.total
        self._on_finish(self.active, summary)
        self.active = None

    def final_summary(self, message: str):
        self._on_note(message)

    def _rate_eta(self, st: PhaseState) -> tuple[float, str]:
        elapsed = max(0.001, time.time() - st.started_at)
        rate = st.current / elapsed
        if rate <= 0:
            return 0.0, "--"
        remaining = max(0, st.total - st.current)
        eta_seconds = int(remaining / rate)
        mins, secs = divmod(eta_seconds, 60)
        hours, mins = divmod(mins, 60)
        if hours > 0:
            eta = f"{hours}h{mins:02d}m"
        elif mins > 0:
            eta = f"{mins}m{secs:02d}s"
        else:
            eta = f"{secs}s"
        return rate, eta

    def _format_line(self, st: PhaseState) -> str:
        pct = (st.current / st.total) * 100
        rate, eta = self._rate_eta(st)
        return (
            f"{st.name}: {st.current}/{st.total} ({pct:5.1f}%) | "
            f"{rate:4.2f}/s | ETA {eta} | retries {st.retries_total} | failures {st.failures}"
        )

    def _on_start(self, st: PhaseState):
        raise NotImplementedError

    def _on_advance(self, st: PhaseState):
        raise NotImplementedError

    def _on_finish(self, st: PhaseState, summary: str):
        raise NotImplementedError

    def _on_note(self, message: str):
        raise NotImplementedError


class PlainProgressReporter(BaseProgressReporter):
    def _on_start(self, st: PhaseState):
        print(f"[{st.name}] start total={st.total}")

    def _on_advance(self, st: PhaseState):
        if st.current == st.total or st.current % self.log_every == 0:
            print(f"[{st.name}] {self._format_line(st)}")

    def _on_finish(self, st: PhaseState, summary: str):
        line = self._format_line(st)
        if summary:
            line = f"{line} | {summary}"
        print(f"[{st.name}] done {line}")

    def _on_note(self, message: str):
        print(message)


class RichProgressReporter(BaseProgressReporter):
    def __init__(self, log_every: int = 25):
        super().__init__(log_every=log_every)
        self.console = Console() if Console else None
        self.progress = (
            Progress(
                SpinnerColumn(),
                TextColumn("[bold cyan]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                TextColumn("{task.fields[stats]}"),
                TimeElapsedColumn(),
                TimeRemainingColumn(),
                transient=False,
            )
            if Progress
            else None
        )
        self.task_id: int | None = None
        if self.progress:
            self.progress.start()

    def _on_start(self, st: PhaseState):
        if not self.progress:
            return
        self.task_id = self.progress.add_task(st.name, total=st.total, stats="")

    def _on_advance(self, st: PhaseState):
        if not self.progress or self.task_id is None:
            return
        rate, eta = self._rate_eta(st)
        stats = f"{st.current}/{st.total} | {rate:4.2f}/s | ETA {eta} | r:{st.retries_total} f:{st.failures}"
        self.progress.update(self.task_id, completed=st.current, stats=stats)

    def _on_finish(self, st: PhaseState, summary: str):
        if self.progress and self.task_id is not None:
            self._on_advance(st)
            if summary and self.console:
                self.console.print(f"[green]{st.name} complete[/green] - {summary}")
            self.progress.remove_task(self.task_id)
            self.task_id = None

    def _on_note(self, message: str):
        if self.console:
            self.console.print(message)
        else:
            print(message)

    def close(self):
        if self.progress:
            self.progress.stop()


def create_progress_reporter() -> BaseProgressReporter:
    mode = os.getenv("PROGRESS_MODE", "auto").strip().lower()
    log_every = int(os.getenv("PROGRESS_LOG_EVERY", "25"))
    if mode not in {"auto", "rich", "plain"}:
        mode = "auto"

    is_ci = os.getenv("GITHUB_ACTIONS", "").lower() == "true"
    is_tty = sys.stdout.isatty()

    use_rich = mode == "rich" or (mode == "auto" and is_tty and not is_ci)
    if use_rich and (Console is None or Progress is None):
        print("rich is unavailable, falling back to plain progress")
        use_rich = False

    if use_rich:
        return RichProgressReporter(log_every=log_every)
    return PlainProgressReporter(log_every=log_every)


class NotionClient:
    def __init__(
        self,
        token: str,
        notion_version: str,
        max_rps: float,
        retries: int,
        timeout_connect: int,
        timeout_read: int,
        retry_backoff_seconds: list[int],
    ):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Notion-Version": notion_version,
                "Content-Type": "application/json",
            }
        )
        self.min_interval = 1.0 / max(max_rps, 0.1)
        self.last_ts = 0.0
        self.retries = max(1, retries)
        self.timeout = (max(1, timeout_connect), max(1, timeout_read))
        self.retry_backoff_seconds = retry_backoff_seconds or [2, 5, 15, 30, 60]
        self.telemetry = {
            "retry_total": 0,
            "retry_http": 0,
            "retry_network": 0,
            "timeout_count": 0,
        }

    def _wait(self):
        now = time.time()
        delta = now - self.last_ts
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self.last_ts = time.time()

    def _next_delay(self, attempt: int, retry_after: str | None = None) -> float:
        if retry_after:
            try:
                return max(1.0, float(retry_after))
            except Exception:
                pass
        idx = min(attempt - 1, len(self.retry_backoff_seconds) - 1)
        base = float(self.retry_backoff_seconds[idx])
        jitter = random.uniform(0, min(1.0, base * 0.2))
        return min(120.0, base + jitter)

    def request(self, method: str, path: str, **kwargs):
        url = f"{API_BASE}{path}"
        for attempt in range(1, self.retries + 1):
            self._wait()
            try:
                res = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except (ReadTimeout, ConnectTimeout, ConnectionError, ChunkedEncodingError, Timeout) as e:
                if isinstance(e, (ReadTimeout, ConnectTimeout, Timeout)):
                    self.telemetry["timeout_count"] += 1
                if attempt < self.retries:
                    self.telemetry["retry_total"] += 1
                    self.telemetry["retry_network"] += 1
                    delay = self._next_delay(attempt)
                    time.sleep(delay)
                    continue
                raise

            if res.status_code < 400:
                return res

            is_retryable = res.status_code in {429, 500, 502, 503, 504}
            if is_retryable and attempt < self.retries:
                self.telemetry["retry_total"] += 1
                self.telemetry["retry_http"] += 1
                delay = self._next_delay(attempt, res.headers.get("Retry-After"))
                time.sleep(delay)
                continue

            raise RuntimeError(f"Notion API error {res.status_code} for {method} {path}: {res.text[:400]}")

        raise RuntimeError(f"Unreachable retries exhausted for {method} {path}")

    def paginate_post(self, path: str, body: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        body = body or {}
        results: list[dict[str, Any]] = []
        cursor = None
        while True:
            payload = {**body, "page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            res = self.request("POST", path, json=payload)
            data = res.json()
            results.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return results


def load_config(config_path: Path) -> Config:
    cfg = Config(exclude_patterns=[])
    if not config_path.exists():
        return cfg
    if yaml is None:
        print("Warning: PyYAML not installed, using defaults", file=sys.stderr)
        return cfg

    with config_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    cfg.max_rps = float(raw.get("max_requests_per_second", cfg.max_rps))
    retry = raw.get("retry", {}) or {}
    cfg.retries = int(retry.get("max_attempts", cfg.retries))
    cfg.retry_backoff_seconds = [int(x) for x in (retry.get("backoff_seconds", cfg.retry_backoff_seconds) or cfg.retry_backoff_seconds)]
    cfg.exclude_patterns = list(raw.get("exclude_patterns", cfg.exclude_patterns) or [])
    cfg.slug_strategy = str(raw.get("slug_strategy", cfg.slug_strategy))

    block_raw = raw.get("full_block_json", {}) or {}
    cfg.block_json = BlockJsonConfig(
        enabled=bool(block_raw.get("enabled", True)),
        frequency=str(block_raw.get("frequency", "weekly")).lower(),
        compression=str(block_raw.get("compression", "gzip")).lower(),
        retention_count=int(block_raw.get("retention_count", 12)),
    )

    cfg.request_timeout_connect_seconds = int(raw.get("request_timeout_connect_seconds", cfg.request_timeout_connect_seconds))
    cfg.request_timeout_read_seconds = int(raw.get("request_timeout_read_seconds", cfg.request_timeout_read_seconds))
    cfg.failure_mode = str(raw.get("failure_mode", cfg.failure_mode)).lower()
    cfg.failure_threshold_percent = float(raw.get("failure_threshold_percent", cfg.failure_threshold_percent))
    cfg.max_consecutive_failures = int(raw.get("max_consecutive_failures", cfg.max_consecutive_failures))
    cfg.staging_keep = int(raw.get("staging_keep", cfg.staging_keep))
    cfg.checkpoint_enabled = bool(raw.get("checkpoint_enabled", cfg.checkpoint_enabled))
    cfg.checkpoint_flush_every = int(raw.get("checkpoint_flush_every", cfg.checkpoint_flush_every))

    return cfg


def normalize_id(raw: str) -> str:
    return raw.replace("-", "").lower()


def extract_notion_id(text: str | None) -> str | None:
    if not text:
        return None
    m1 = UUID_DASHED_RE.search(text)
    if m1:
        return normalize_id(m1.group(1))
    m2 = UUID_32_RE.search(text)
    if m2:
        return normalize_id(m2.group(1))
    return None


def slugify(s: str) -> str:
    if not s:
        return "untitled"
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9\s\-_]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s[:80].strip("-") or "untitled"


def title_from_page(page: dict[str, Any]) -> str:
    props = page.get("properties", {})
    for prop in props.values():
        if prop.get("type") == "title":
            parts = prop.get("title", [])
            return "".join(p.get("plain_text", "") for p in parts).strip() or "Untitled"
    return "Untitled"


def title_from_data_source(ds: dict[str, Any], fallback_id: str) -> str:
    title = ds.get("title") if isinstance(ds, dict) else None
    if isinstance(title, list):
        t = "".join(x.get("plain_text", "") for x in title).strip()
        if t:
            return t
    if isinstance(title, str) and title.strip():
        return title.strip()
    return f"Data Source {fallback_id[:8]}"


def is_excluded(value: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, value) for pattern in patterns)


def property_to_string(prop: dict[str, Any]) -> str:
    ptype = prop.get("type")
    val = prop.get(ptype)
    if ptype in {"title", "rich_text"}:
        return "".join(x.get("plain_text", "") for x in val or [])
    if ptype == "number":
        return "" if val is None else str(val)
    if ptype == "checkbox":
        return "true" if val else "false"
    if ptype == "url":
        return val or ""
    if ptype == "email":
        return val or ""
    if ptype == "phone_number":
        return val or ""
    if ptype == "select":
        return (val or {}).get("name", "")
    if ptype == "multi_select":
        return ", ".join(x.get("name", "") for x in val or [])
    if ptype == "status":
        return (val or {}).get("name", "")
    if ptype == "date":
        if not val:
            return ""
        start = val.get("start") or ""
        end = val.get("end") or ""
        return f"{start}..{end}" if end else start
    if ptype in {"created_time", "last_edited_time"}:
        return val or ""
    if ptype in {"created_by", "last_edited_by"}:
        return (val or {}).get("id", "")
    if ptype in {"people", "relation"}:
        return ", ".join(x.get("id", "") for x in val or [])
    return json.dumps(val, ensure_ascii=False)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def safe_write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def rel(path: Path, root: Path) -> str:
    return str(path.relative_to(root)).replace("\\", "/")


def fetch_children(client: NotionClient, block_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor = None
    while True:
        query = f"?page_size=100"
        if cursor:
            query += f"&start_cursor={cursor}"
        res = client.request("GET", f"/blocks/{block_id}/children{query}")
        data = res.json()
        out.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return out


def block_to_markdown(block: dict[str, Any], depth: int = 0) -> list[str]:
    btype = block.get("type")
    data = block.get(btype, {}) if btype else {}

    def text(rich: list[dict[str, Any]]) -> str:
        return "".join(t.get("plain_text", "") for t in rich or [])

    out: list[str] = []
    if btype == "paragraph":
        out.append(text(data.get("rich_text", [])) or "")
    elif btype == "heading_1":
        out.append(f"# {text(data.get('rich_text', []))}")
    elif btype == "heading_2":
        out.append(f"## {text(data.get('rich_text', []))}")
    elif btype == "heading_3":
        out.append(f"### {text(data.get('rich_text', []))}")
    elif btype == "bulleted_list_item":
        out.append(f"{'  ' * depth}- {text(data.get('rich_text', []))}")
    elif btype == "numbered_list_item":
        out.append(f"{'  ' * depth}1. {text(data.get('rich_text', []))}")
    elif btype == "to_do":
        checked = "x" if data.get("checked") else " "
        out.append(f"{'  ' * depth}- [{checked}] {text(data.get('rich_text', []))}")
    elif btype == "quote":
        out.append(f"> {text(data.get('rich_text', []))}")
    elif btype == "code":
        lang = data.get("language", "")
        out.append(f"```{lang}\n{text(data.get('rich_text', []))}\n```")
    elif btype == "divider":
        out.append("---")
    elif btype == "callout":
        out.append(f"> {text(data.get('rich_text', []))}")
    elif btype == "child_page":
        out.append(f"## {data.get('title', 'Child Page')}")
    elif btype == "bookmark":
        out.append(data.get("url", ""))
    else:
        fallback = text(data.get("rich_text", []))
        if fallback:
            out.append(fallback)
    return out


def fetch_blocks_markdown(client: NotionClient, block_id: str, depth: int = 0) -> list[str]:
    lines: list[str] = []
    for block in fetch_children(client, block_id):
        lines.extend(block_to_markdown(block, depth))
        if block.get("has_children"):
            lines.extend(fetch_blocks_markdown(client, block["id"], depth + 1))
    return lines


def fetch_blocks_tree(client: NotionClient, block_id: str) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for block in fetch_children(client, block_id):
        node = dict(block)
        if block.get("has_children"):
            node["_children"] = fetch_blocks_tree(client, block["id"])
        nodes.append(node)
    return nodes


def page_markdown(client: NotionClient, page_id: str) -> str:
    try:
        res = client.request("GET", f"/pages/{page_id}/markdown")
        data = res.json()
        md = data.get("page_markdown") or data.get("markdown") or ""
        if md:
            return md
    except Exception:
        pass
    lines = fetch_blocks_markdown(client, page_id)
    return "\n\n".join(lines).strip() + "\n"


def relative_md_link(src_path: Path, target_path: Path) -> str:
    rel_path = os.path.relpath(target_path, src_path.parent)
    return rel_path.replace("\\", "/")


def clean_label(text: str, fallback: str) -> str:
    txt = re.sub(r"<[^>]+>", "", text or "")
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt or fallback


def rewrite_links_in_markdown(content: str, src_path: Path, id_to_md_path: dict[str, Path], unresolved: list[dict[str, str]]) -> tuple[str, int]:
    resolved_count = 0

    def resolve(id_or_url: str) -> Path | None:
        pid = extract_notion_id(id_or_url)
        if not pid:
            return None
        return id_to_md_path.get(pid)

    def replace_md_link(match: re.Match) -> str:
        nonlocal resolved_count
        label = match.group("label")
        url = match.group("url")
        target = resolve(url)
        if target:
            resolved_count += 1
            return f"[{label}]({relative_md_link(src_path, target)})"
        if "notion.so" in url:
            unresolved.append({"file": str(src_path), "url": url, "type": "markdown_link"})
        return match.group(0)

    content = MARKDOWN_LINK_RE.sub(replace_md_link, content)

    def replace_database_tag(match: re.Match) -> str:
        nonlocal resolved_count
        attrs = match.group("attrs") or ""
        label = clean_label(match.group("label") or "", "Database")
        ds_match = re.search(r'data-source-url="collection://([^"]+)"', attrs)
        url_match = re.search(r'url="([^"]+)"', attrs)
        ds_id = extract_notion_id(ds_match.group(1) if ds_match else "")
        url = url_match.group(1) if url_match else ""

        target = id_to_md_path.get(ds_id) if ds_id else None
        if not target and url:
            target = resolve(url)
        if target:
            resolved_count += 1
            return f"[{label}]({relative_md_link(src_path, target)})"
        if url:
            unresolved.append({"file": str(src_path), "url": url, "type": "database_tag"})
            return f"[{label}]({url})"
        return label

    content = DATABASE_TAG_RE.sub(replace_database_tag, content)

    def replace_mention_pair(match: re.Match) -> str:
        nonlocal resolved_count
        attrs = match.group("attrs") or ""
        inner = match.group("label") or ""
        label = clean_label(inner, "Linked page")
        url_match = re.search(r'url="([^"]+)"', attrs)
        url = url_match.group(1) if url_match else ""
        target = resolve(url)
        if target:
            resolved_count += 1
            return f"[{label}]({relative_md_link(src_path, target)})"
        if url:
            unresolved.append({"file": str(src_path), "url": url, "type": "mention_page"})
            return f"[{label}]({url})"
        return label

    content = MENTION_PAGE_PAIR_RE.sub(replace_mention_pair, content)

    def replace_mention_self(match: re.Match) -> str:
        nonlocal resolved_count
        attrs = match.group("attrs") or ""
        url_match = re.search(r'url="([^"]+)"', attrs)
        url = url_match.group(1) if url_match else ""
        target = resolve(url)
        pid = extract_notion_id(url)
        fallback_label = pid[:8] if pid else "linked-page"
        if target:
            resolved_count += 1
            return f"[{fallback_label}]({relative_md_link(src_path, target)})"
        if url:
            unresolved.append({"file": str(src_path), "url": url, "type": "mention_page"})
            return f"[{fallback_label}]({url})"
        return "linked-page"

    content = MENTION_PAGE_SELF_RE.sub(replace_mention_self, content)

    def replace_generic_url_tag(match: re.Match) -> str:
        nonlocal resolved_count
        tag = (match.group("tag") or "link").lower()
        url = match.group("url")
        target = resolve(url)
        label = tag.replace("_", "-")
        if target:
            resolved_count += 1
            return f"[{label}]({relative_md_link(src_path, target)})"
        if "notion.so" in url:
            unresolved.append({"file": str(src_path), "url": url, "type": f"{tag}_tag"})
        return f"[{label}]({url})"

    content = GENERIC_URL_TAG_RE.sub(replace_generic_url_tag, content)
    return content, resolved_count


def should_generate_block_json(cfg: BlockJsonConfig) -> bool:
    if not cfg.enabled:
        return False
    if os.getenv("FULL_BLOCK_JSON_FORCE", "").strip() == "1":
        return True
    if cfg.frequency == "daily":
        return True
    if cfg.frequency == "weekly":
        return dt.datetime.now(dt.UTC).weekday() == 6
    return False


def cleanup_old_block_snapshots(blocks_root: Path, keep: int):
    if keep <= 0 or not blocks_root.exists():
        return
    dirs = sorted([p for p in blocks_root.iterdir() if p.is_dir() and re.match(r"\d{4}-\d{2}-\d{2}", p.name)])
    for old in dirs[:-keep]:
        shutil.rmtree(old, ignore_errors=True)


def cleanup_staging(staging_root: Path, keep: int):
    if not staging_root.exists() or keep <= 0:
        return
    dirs = sorted([p for p in staging_root.iterdir() if p.is_dir()])
    for old in dirs[:-keep]:
        shutil.rmtree(old, ignore_errors=True)


def load_checkpoint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"completed_page_ids": [], "completed_data_source_ids": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"completed_page_ids": [], "completed_data_source_ids": []}


def save_checkpoint(path: Path, data: dict[str, Any]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def now_utc() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def maybe_raise_for_mode(cfg: Config, failures: list[dict[str, Any]], discovered: int):
    if cfg.failure_mode == "strict" and failures:
        raise RuntimeError(f"Strict mode: encountered {len(failures)} failures")
    if cfg.failure_mode == "threshold" and discovered > 0:
        pct = (len(failures) / discovered) * 100
        if pct > cfg.failure_threshold_percent:
            raise RuntimeError(
                f"Threshold mode: failure rate {pct:.2f}% exceeds {cfg.failure_threshold_percent}%"
            )


def promote_staging(stage_backups: Path, canonical_backups: Path):
    targets = ["notion-md", "notion-json", "manifests"]
    for name in targets:
        src = stage_backups / name
        dst = canonical_backups / name
        if not src.exists():
            continue
        if dst.exists():
            shutil.rmtree(dst)
        shutil.move(str(src), str(dst))


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if load_dotenv is not None:
        load_dotenv(repo_root / ".env", override=False)
    cfg = load_config(repo_root / "backup.config.yaml")

    token = os.getenv("NOTION_API_KEY")
    if not token:
        print("NOTION_API_KEY is required", file=sys.stderr)
        return 2

    notion_version = os.getenv("NOTION_NOTION_VERSION", DEFAULT_NOTION_VERSION)
    run_started = time.time()
    run_id = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")

    canonical_backups = repo_root / "backups"
    staging_root = canonical_backups / ".staging"
    stage_run_root = staging_root / run_id
    stage_backups = stage_run_root / "backups"

    md_root = stage_backups / "notion-md"
    json_root = stage_backups / "notion-json"
    manifest_root = stage_backups / "manifests"
    manifest_root.mkdir(parents=True, exist_ok=True)

    # Preserve historical blocks snapshots across staging runs.
    existing_blocks = canonical_backups / "notion-json" / "blocks"
    if existing_blocks.exists():
        shutil.copytree(existing_blocks, json_root / "blocks", dirs_exist_ok=True)

    checkpoint_path = canonical_backups / ".state" / "export_progress.json"
    checkpoint = load_checkpoint(checkpoint_path) if cfg.checkpoint_enabled else {"completed_page_ids": [], "completed_data_source_ids": []}
    completed_page_ids = set(checkpoint.get("completed_page_ids", []))
    completed_data_source_ids = set(checkpoint.get("completed_data_source_ids", []))

    failures: list[dict[str, Any]] = []
    run_summary: dict[str, Any] = {
        "run_id": run_id,
        "started_at": now_utc(),
        "notion_version": notion_version,
        "failure_mode": cfg.failure_mode,
        "counts": {
            "pages_discovered": 0,
            "pages_succeeded": 0,
            "pages_failed": 0,
            "pages_skipped_checkpoint": 0,
            "data_sources_discovered": 0,
            "data_sources_succeeded": 0,
            "data_sources_failed": 0,
            "data_sources_skipped_checkpoint": 0,
            "link_files_processed": 0,
            "link_rewrite_failures": 0,
            "resolved_links": 0,
            "unresolved_links": 0,
        },
        "retries": {},
    }

    client = NotionClient(
        token=token,
        notion_version=notion_version,
        max_rps=cfg.max_rps,
        retries=cfg.retries,
        timeout_connect=cfg.request_timeout_connect_seconds,
        timeout_read=cfg.request_timeout_read_seconds,
        retry_backoff_seconds=cfg.retry_backoff_seconds,
    )
    reporter = create_progress_reporter()

    root_page_ids: list[str] = []
    created_files: set[Path] = set()
    manifest_entries: list[dict[str, Any]] = []
    page_records: list[dict[str, Any]] = []
    page_ids_by_data_source: dict[str, list[str]] = {}
    unresolved_links: list[dict[str, str]] = []

    def checkpoint_flush(force: bool = False):
        if not cfg.checkpoint_enabled:
            return
        total_done = len(completed_page_ids) + len(completed_data_source_ids)
        if force or (total_done % max(1, cfg.checkpoint_flush_every) == 0):
            save_checkpoint(
                checkpoint_path,
                {
                    "run_id": run_id,
                    "updated_at": now_utc(),
                    "completed_page_ids": sorted(completed_page_ids),
                    "completed_data_source_ids": sorted(completed_data_source_ids),
                },
            )

    try:
        reporter.note("Discovering pages and data sources...")
        reporter.start_phase("Discovery", 1)
        search_results = client.paginate_post(
            "/search", body={"query": "", "sort": {"direction": "descending", "timestamp": "last_edited_time"}}
        )
        reporter.advance(retries_total=client.telemetry["retry_total"])
        reporter.finish_phase("Search completed")

        pages = [x for x in search_results if x.get("object") == "page"]
        run_summary["counts"]["pages_discovered"] = len(pages)

        data_source_ids: set[str] = set()
        for page in pages:
            parent = page.get("parent", {})
            if parent.get("type") == "data_source_id":
                ds = parent.get("data_source_id") or parent.get("database_id")
                if ds:
                    data_source_ids.add(normalize_id(ds))
        run_summary["counts"]["data_sources_discovered"] = len(data_source_ids)
        reporter.note(f"Found {len(pages)} pages and {len(data_source_ids)} data sources")

        consecutive_failures = 0
        reporter.start_phase("Page export", len(pages))
        for page in pages:
            page_id = normalize_id(page["id"])
            if cfg.checkpoint_enabled and page_id in completed_page_ids:
                run_summary["counts"]["pages_skipped_checkpoint"] += 1
                reporter.advance(retries_total=client.telemetry["retry_total"], failures=run_summary["counts"]["pages_failed"])
                continue

            title = title_from_page(page)
            if is_excluded(title, cfg.exclude_patterns or []):
                completed_page_ids.add(page_id)
                checkpoint_flush()
                reporter.advance(retries_total=client.telemetry["retry_total"], failures=run_summary["counts"]["pages_failed"])
                continue

            slug = f"{slugify(title)}--{page_id[:8]}"
            parent = page.get("parent", {})
            parent_type = parent.get("type", "unknown")

            if parent_type == "workspace":
                root_page_ids.append(page_id)
                folder = md_root / "workspace"
            elif parent_type == "page_id":
                folder = md_root / "pages"
            elif parent_type in {"database_id", "data_source_id"}:
                folder = md_root / "database-pages"
                dsid = normalize_id(parent.get("data_source_id") or parent.get("database_id") or "")
                if dsid:
                    page_ids_by_data_source.setdefault(dsid, []).append(page_id)
            else:
                folder = md_root / "other"

            try:
                md_path = folder / f"{slug}.md"
                content = page_markdown(client, page["id"])
                safe_write(md_path, content)
                created_files.add(md_path)

                meta = {
                    "id": page_id,
                    "object": "page",
                    "title": title,
                    "last_edited_time": page.get("last_edited_time"),
                    "url": page.get("url"),
                    "parent": parent,
                }
                meta_path = json_root / "pages" / f"{slug}.json"
                safe_write(meta_path, json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
                created_files.add(meta_path)

                page_records.append(
                    {
                        "id": page_id,
                        "slug": slug,
                        "title": title,
                        "parent_type": parent_type,
                        "url": page.get("url"),
                        "md_path": md_path,
                        "last_edited_time": page.get("last_edited_time"),
                    }
                )
                completed_page_ids.add(page_id)
                run_summary["counts"]["pages_succeeded"] += 1
                consecutive_failures = 0
            except Exception as e:
                run_summary["counts"]["pages_failed"] += 1
                consecutive_failures += 1
                failures.append(
                    {
                        "kind": "page",
                        "id": page_id,
                        "title": title,
                        "stage": "page_export",
                        "endpoint": f"/pages/{page.get('id')}/markdown or /blocks/*/children",
                        "error_type": type(e).__name__,
                        "error": str(e),
                    }
                )
                if cfg.failure_mode == "strict" or consecutive_failures >= cfg.max_consecutive_failures:
                    raise
            finally:
                checkpoint_flush()
                reporter.advance(retries_total=client.telemetry["retry_total"], failures=run_summary["counts"]["pages_failed"])

        reporter.finish_phase(
            f"ok={run_summary['counts']['pages_succeeded']} failed={run_summary['counts']['pages_failed']} skipped={run_summary['counts']['pages_skipped_checkpoint']}"
        )

        data_source_records: list[dict[str, Any]] = []
        reporter.start_phase("Data source export", len(data_source_ids))
        consecutive_failures = 0
        for dsid in sorted(data_source_ids):
            if cfg.checkpoint_enabled and dsid in completed_data_source_ids:
                run_summary["counts"]["data_sources_skipped_checkpoint"] += 1
                reporter.advance(retries_total=client.telemetry["retry_total"], failures=run_summary["counts"]["data_sources_failed"])
                continue

            ds_resp = None
            title = f"Data Source {dsid[:8]}"
            last_edited = None
            rows: list[dict[str, Any]] = []

            try:
                try:
                    ds_resp = client.request("GET", f"/data-sources/{dsid.replace('-', '')}").json()
                    title = title_from_data_source(ds_resp, dsid)
                    last_edited = ds_resp.get("last_edited_time")
                except Exception:
                    pass

                slug = f"{slugify(title)}--{dsid[:8]}"
                try:
                    rows = client.paginate_post(f"/data-sources/{dsid}/query", body={})
                except Exception:
                    rows = []

                flat_rows: list[dict[str, str]] = []
                headers: set[str] = set()
                for row in rows:
                    props = row.get("properties", {})
                    flat = {
                        "id": normalize_id(row.get("id", "")),
                        "last_edited_time": row.get("last_edited_time", ""),
                    }
                    for name, prop in props.items():
                        val = property_to_string(prop)
                        flat[name] = val
                        headers.add(name)
                    flat_rows.append(flat)

                csv_headers = ["id", "last_edited_time", *sorted(headers)]
                csv_path = json_root / "data-sources" / f"{slug}.csv"
                csv_path.parent.mkdir(parents=True, exist_ok=True)
                with csv_path.open("w", encoding="utf-8", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=csv_headers)
                    writer.writeheader()
                    writer.writerows(flat_rows)
                created_files.add(csv_path)

                raw_path = json_root / "data-sources" / f"{slug}.json"
                safe_write(raw_path, json.dumps({"data_source": ds_resp, "rows": rows}, ensure_ascii=False, indent=2) + "\n")
                created_files.add(raw_path)

                ds_md_path = md_root / "data-sources" / f"{slug}.md"
                members = page_ids_by_data_source.get(dsid, [])
                lines = [f"# {title}", "", f"Data Source ID: `{dsid}`", "", f"Entries discovered: {len(members)}", ""]
                if members:
                    lines.append("## Entries")
                    lines.append("")
                    for pid in sorted(set(members)):
                        rec = next((r for r in page_records if r["id"] == pid), None)
                        if rec:
                            lines.append(f"- [{rec['title']}](../database-pages/{rec['slug']}.md)")
                safe_write(ds_md_path, "\n".join(lines) + "\n")
                created_files.add(ds_md_path)

                data_source_records.append(
                    {
                        "id": dsid,
                        "slug": slug,
                        "title": title,
                        "row_count": len(rows),
                        "md_path": ds_md_path,
                        "json_path": raw_path,
                        "last_edited_time": last_edited,
                    }
                )

                completed_data_source_ids.add(dsid)
                run_summary["counts"]["data_sources_succeeded"] += 1
                consecutive_failures = 0
            except Exception as e:
                run_summary["counts"]["data_sources_failed"] += 1
                consecutive_failures += 1
                failures.append(
                    {
                        "kind": "data_source",
                        "id": dsid,
                        "title": title,
                        "stage": "data_source_export",
                        "endpoint": f"/data-sources/{dsid}/*",
                        "error_type": type(e).__name__,
                        "error": str(e),
                    }
                )
                if cfg.failure_mode == "strict" or consecutive_failures >= cfg.max_consecutive_failures:
                    raise
            finally:
                checkpoint_flush()
                reporter.advance(retries_total=client.telemetry["retry_total"], failures=run_summary["counts"]["data_sources_failed"])

        reporter.finish_phase(
            f"ok={run_summary['counts']['data_sources_succeeded']} failed={run_summary['counts']['data_sources_failed']} skipped={run_summary['counts']['data_sources_skipped_checkpoint']}"
        )

        id_to_md_path: dict[str, Path] = {r["id"]: r["md_path"] for r in page_records}
        for ds in data_source_records:
            id_to_md_path[ds["id"]] = ds["md_path"]

        md_files = sorted(md_root.rglob("*.md"))
        reporter.start_phase("Link rewrite", len(md_files))
        resolved_links = 0
        for md_path in md_files:
            try:
                src = md_path.read_text(encoding="utf-8")
                rewritten, resolved = rewrite_links_in_markdown(src, md_path, id_to_md_path, unresolved_links)
                resolved_links += resolved
                if rewritten != src:
                    safe_write(md_path, rewritten)
                created_files.add(md_path)
            except Exception as e:
                run_summary["counts"]["link_rewrite_failures"] += 1
                failures.append(
                    {
                        "kind": "link_rewrite",
                        "id": str(md_path),
                        "stage": "link_rewrite",
                        "error_type": type(e).__name__,
                        "error": str(e),
                    }
                )
            finally:
                run_summary["counts"]["link_files_processed"] += 1
                reporter.advance(retries_total=client.telemetry["retry_total"], failures=run_summary["counts"]["link_rewrite_failures"])

        run_summary["counts"]["resolved_links"] = resolved_links
        run_summary["counts"]["unresolved_links"] = len(unresolved_links)
        reporter.finish_phase(
            f"resolved={resolved_links} unresolved={len(unresolved_links)} rewrite_failures={run_summary['counts']['link_rewrite_failures']}"
        )

        if should_generate_block_json(cfg.block_json):
            today_utc = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")
            block_snapshot_dir = json_root / "blocks" / today_utc
            block_snapshot_dir.mkdir(parents=True, exist_ok=True)
            snapshot_index: list[dict[str, Any]] = []

            reporter.start_phase("Block snapshots", len(page_records))
            for rec in page_records:
                try:
                    blocks = fetch_blocks_tree(client, rec["id"])
                    payload = {
                        "id": rec["id"],
                        "title": rec["title"],
                        "generated_at": now_utc(),
                        "blocks": blocks,
                    }
                    if cfg.block_json.compression == "gzip":
                        out_path = block_snapshot_dir / f"{rec['slug']}.json.gz"
                        with gzip.open(out_path, "wt", encoding="utf-8") as gz:
                            gz.write(json.dumps(payload, ensure_ascii=False))
                    else:
                        out_path = block_snapshot_dir / f"{rec['slug']}.json"
                        safe_write(out_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

                    snapshot_index.append(
                        {
                            "id": rec["id"],
                            "slug": rec["slug"],
                            "path": rel(out_path, repo_root),
                            "bytes": out_path.stat().st_size,
                            "sha256": sha256_file(out_path),
                        }
                    )
                    created_files.add(out_path)
                except Exception as e:
                    failures.append(
                        {
                            "kind": "block_snapshot",
                            "id": rec["id"],
                            "stage": "block_snapshot",
                            "error_type": type(e).__name__,
                            "error": str(e),
                        }
                    )
                finally:
                    reporter.advance(retries_total=client.telemetry["retry_total"], failures=len(failures))

            index_path = block_snapshot_dir / "index.json"
            safe_write(index_path, json.dumps({"generated_at": now_utc(), "items": snapshot_index}, ensure_ascii=False, indent=2) + "\n")
            created_files.add(index_path)
            cleanup_old_block_snapshots(json_root / "blocks", cfg.block_json.retention_count)
            reporter.finish_phase(f"snapshot_items={len(snapshot_index)}")

        reporter.start_phase("Manifest build", len(page_records) + len(data_source_records))
        for rec in page_records:
            manifest_entries.append(
                {
                    "canonical_id": rec["id"],
                    "id": rec["id"],
                    "object": "page",
                    "source_type": "page",
                    "slug": rec["slug"],
                    "title": rec["title"],
                    "parent_type": rec["parent_type"],
                    "path": rel(rec["md_path"], repo_root),
                    "md_path": rel(rec["md_path"], repo_root),
                    "html_path": rel((repo_root / "backups" / "site" / rec["md_path"].relative_to(md_root)).with_suffix(".html"), repo_root),
                    "sha256": sha256_file(rec["md_path"]),
                    "last_edited_time": rec["last_edited_time"],
                }
            )
            reporter.advance(retries_total=client.telemetry["retry_total"], failures=len(failures))

        for ds in data_source_records:
            manifest_entries.append(
                {
                    "canonical_id": ds["id"],
                    "id": ds["id"],
                    "object": "data_source",
                    "source_type": "data_source",
                    "slug": ds["slug"],
                    "title": ds["title"],
                    "path": rel(ds["json_path"], repo_root),
                    "md_path": rel(ds["md_path"], repo_root),
                    "html_path": rel((repo_root / "backups" / "site" / ds["md_path"].relative_to(md_root)).with_suffix(".html"), repo_root),
                    "sha256": sha256_file(ds["json_path"]),
                    "row_count": ds["row_count"],
                    "last_edited_time": ds["last_edited_time"],
                }
            )
            reporter.advance(retries_total=client.telemetry["retry_total"], failures=len(failures))
        reporter.finish_phase(f"entries={len(manifest_entries)}")

        # Validate failure policy and health checks before promotion.
        maybe_raise_for_mode(cfg, failures, max(1, len(pages) + len(data_source_ids)))

        previous_manifest_path = canonical_backups / "manifests" / "manifest.json"
        prev_count = None
        if previous_manifest_path.exists():
            try:
                prev = json.loads(previous_manifest_path.read_text(encoding="utf-8"))
                prev_count = int(prev.get("object_count", 0))
            except Exception:
                prev_count = None

        if prev_count is not None and prev_count >= 20 and len(manifest_entries) < max(5, int(prev_count * 0.2)):
            raise RuntimeError(
                f"Health guard triggered: previous object_count={prev_count}, current={len(manifest_entries)}"
            )

        manifest = {
            "generated_at": now_utc(),
            "notion_version": notion_version,
            "object_count": len(manifest_entries),
            "entries": sorted(manifest_entries, key=lambda x: (x["object"], x["id"])),
        }
        safe_write(manifest_root / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        safe_write(manifest_root / "roots.json", json.dumps({"generated_at": manifest["generated_at"], "root_page_ids": sorted(set(root_page_ids))}, ensure_ascii=False, indent=2) + "\n")

        link_index = {
            "generated_at": manifest["generated_at"],
            "targets": {
                k: {
                    "md_path": rel(v, repo_root),
                    "html_path": rel((repo_root / "backups" / "site" / v.relative_to(md_root)).with_suffix(".html"), repo_root),
                }
                for k, v in sorted(id_to_md_path.items())
            },
            "resolved_count": resolved_links,
            "unresolved_count": len(unresolved_links),
            "unresolved": unresolved_links,
        }
        safe_write(manifest_root / "link_index.json", json.dumps(link_index, ensure_ascii=False, indent=2) + "\n")

        run_summary["retries"] = client.telemetry
        run_summary["ended_at"] = now_utc()
        run_summary["duration_seconds"] = round(time.time() - run_started, 2)
        safe_write(manifest_root / "failures.json", json.dumps({"generated_at": now_utc(), "items": failures}, ensure_ascii=False, indent=2) + "\n")
        safe_write(manifest_root / "run_summary.json", json.dumps(run_summary, ensure_ascii=False, indent=2) + "\n")

        promote_staging(stage_backups, canonical_backups)
        cleanup_staging(staging_root, cfg.staging_keep)

        if cfg.checkpoint_enabled and checkpoint_path.exists():
            checkpoint_path.unlink()

        reporter.final_summary(
            f"Export complete. pages_ok={run_summary['counts']['pages_succeeded']} pages_failed={run_summary['counts']['pages_failed']} "
            f"ds_ok={run_summary['counts']['data_sources_succeeded']} ds_failed={run_summary['counts']['data_sources_failed']} "
            f"resolved_links={resolved_links} unresolved_links={len(unresolved_links)} retries={client.telemetry['retry_total']}"
        )
        return 0

    except Exception as e:
        # Persist artifacts into staging for debugging.
        run_summary["retries"] = client.telemetry
        run_summary["ended_at"] = now_utc()
        run_summary["duration_seconds"] = round(time.time() - run_started, 2)
        run_summary["fatal_error"] = {"type": type(e).__name__, "message": str(e)}
        safe_write(manifest_root / "failures.json", json.dumps({"generated_at": now_utc(), "items": failures}, ensure_ascii=False, indent=2) + "\n")
        safe_write(manifest_root / "run_summary.json", json.dumps(run_summary, ensure_ascii=False, indent=2) + "\n")
        reporter.note(f"Export failed: {type(e).__name__}: {e}")
        reporter.note(f"Staging kept for debugging: {stage_run_root}")
        return 1

    finally:
        if isinstance(reporter, RichProgressReporter):
            reporter.close()


if __name__ == "__main__":
    raise SystemExit(main())

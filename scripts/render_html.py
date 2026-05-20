#!/usr/bin/env python3
import html
import re
from pathlib import Path

try:
    import markdown as mdlib
except Exception:
    mdlib = None


def markdown_to_html(text: str) -> str:
    if mdlib:
        return mdlib.markdown(text, extensions=["tables", "fenced_code", "toc"])
    return "<pre>" + html.escape(text) + "</pre>"


def rewrite_md_hrefs_to_html(body: str) -> str:
    # Keep local navigation self-contained in the rendered site.
    return re.sub(r'href="([^"]+?)\\.md(#.*?)?"', lambda m: f'href="{m.group(1)}.html{m.group(2) or ""}"', body)


def page_template(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>{html.escape(title)}</title>
  <style>
    :root {{ --bg:#f7f8fa; --fg:#1f2937; --card:#ffffff; --link:#0f766e; --border:#e5e7eb; }}
    body {{ margin:0; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif; background:var(--bg); color:var(--fg); }}
    main {{ max-width: 980px; margin: 2rem auto; background: var(--card); border:1px solid var(--border); border-radius:12px; padding: 2rem; }}
    a {{ color: var(--link); }}
    pre, code {{ background:#f3f4f6; border-radius:6px; }}
    pre {{ padding: 0.75rem; overflow-x:auto; }}
  </style>
</head>
<body>
  <main>
    {body}
  </main>
</body>
</html>
"""


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    official_root = repo / "backups" / "official"
    notion_md_root = repo / "backups" / "notion-md"
    md_root = official_root if official_root.exists() else notion_md_root
    site_root = repo / "backups" / "site"
    site_root.mkdir(parents=True, exist_ok=True)

    links: list[str] = []
    for md_path in sorted(md_root.rglob("*.md")):
        rel = md_path.relative_to(md_root)
        out = site_root / rel.with_suffix(".html")
        out.parent.mkdir(parents=True, exist_ok=True)

        src = md_path.read_text(encoding="utf-8")
        body = markdown_to_html(src)
        body = rewrite_md_hrefs_to_html(body)
        title = md_path.stem
        out.write_text(page_template(title, body), encoding="utf-8")

        href = rel.with_suffix(".html").as_posix()
        links.append(f"<li><a href=\"{html.escape(href)}\">{html.escape(rel.stem)}</a></li>")

    source_label = md_root.relative_to(repo).as_posix() if md_root.exists() else "missing-source"
    index = page_template(
        "Notion Backup",
        f"<h1>Notion Backup</h1><p>Source: <code>{html.escape(source_label)}</code></p><ul>{''.join(links)}</ul>",
    )
    (site_root / "index.html").write_text(index, encoding="utf-8")
    print(f"Rendered {len(links)} HTML pages from {source_label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

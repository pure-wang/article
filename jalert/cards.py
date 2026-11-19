"""Static card-wall page: every report in ``reports/`` becomes one HTML file.

Why re-parse the Markdown instead of keeping a second database: the ``reports/``
directory already *is* the archive (``output.keep_days`` prunes it, nothing else
does), and a full rebuild of a year of reports takes a few hundred milliseconds.
Deriving the page from them means the page can never drift out of sync with the
reports, and a failed fetch never leaves it half-written.

The emitted file is deliberately self-contained - no CDN, no webfont, no image
host, no external request of any kind. That keeps it working when opened
straight from disk via ``file://`` and makes it publishable by GitHub Pages
exactly as written.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from .score import TIER_TITLES

#: Report section headings (``## 必读（16）``) keyed by tier id.
TIER_SECTIONS = {title: tier for tier, title in TIER_TITLES.items()}

#: Palette slots used by the stylesheet. Known labels keep their semantic
#: colour; anything else falls back to the neutral slot.
THEME_KEYS = ("air", "rs", "chem", "bgc", "ghg", "wx", "gis")
THEME_BY_TOPIC = {
    "空气污染": "air",
    "遥感": "rs",
    "大气化学": "chem",
    "生物地球化学": "bgc",
    "温室气体": "ghg",
    "极端天气": "wx",
    "城市地理信息系统": "gis",
}

#: Journal -> short badge label. Long names would wrap and wreck the cover.
JOURNAL_ABBR = {
    "Atmospheric Chemistry and Physics": "ACP",
    "Atmospheric Environment": "Atmos Environ",
    "Remote Sensing of Environment": "RSE",
    "Environmental Science & Technology": "ES&T",
    "Environmental Research Letters": "ERL",
    "Geophysical Research Letters": "GRL",
    "Journal of Geophysical Research: Atmospheres": "JGR-A",
    "Science Advances": "Sci Adv",
    "Nature Communications": "Nat Commun",
    "Nature Climate Change": "Nat Clim Chang",
    "Nature Geoscience": "Nat Geosci",
    "Nature Sustainability": "Nat Sustain",
    "Nature Cities": "Nat Cities",
}

#: Feeds split subscripts into a separate token ("CO 2", "NO x"), which reads
#: badly in a card title. Repair the common ones.
CHEM_FIXES = (
    ("PM 2.5", "PM2.5"), ("PM 10", "PM10"), ("PM 1", "PM1"),
    ("H 2 O 2", "H₂O₂"), ("N 2 O", "N₂O"), ("CH 4", "CH₄"), ("NH 3", "NH₃"),
    ("CO 2", "CO₂"), ("SO 2", "SO₂"), ("SO 4", "SO₄"), ("NO x", "NOₓ"),
    ("NO 2", "NO₂"), ("NO 3", "NO₃"), ("O 3", "O₃"), ("O 2", "O₂"), ("H 2 O", "H₂O"),
)

_ENTRY_RE = re.compile(r"^\s*###\s+\d+\.\s*\[(.+?)\]\((.+?)\)", re.MULTILINE)
_FRONTMATTER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)


def _clean(text: str) -> str:
    """Normalise feed text for display (subscripts, stray punctuation spaces)."""
    if not text:
        return ""
    for src, dst in CHEM_FIXES:
        text = text.replace(src, dst)
    text = re.sub(r"\s+([,;.)\]])", r"\1", text)
    text = re.sub(r"([(\[])\s+", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\s+:\s+", ": ", text)
    return text.strip()


def _field(block: str, name: str, *, stop: str = "｜") -> str:
    """Read one ``- **名称**：值`` bullet, stopping at the ``｜`` separator."""
    match = re.search(rf"\*\*{name}\*\*：\s*(.+?)(?:\s*{stop}|\s*$)", block, re.MULTILINE)
    return match.group(1).strip() if match else ""


def _sort_date(value: str) -> str:
    """Strip the ``（在线预发表）`` suffix so dates compare as plain ISO strings."""
    return (value or "").split("（")[0].strip()


def parse_report(path: Path) -> tuple[dict, list[dict]]:
    """Parse one Markdown report into ``(meta, entries)``.

    A malformed report yields whatever could be read rather than raising: one
    bad file in a year of archives must not take the whole page down.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    day = path.stem

    meta: dict = {"date": day, "topics": [], "generated": ""}
    front = _FRONTMATTER_RE.match(text)
    body = text
    if front:
        body = text[front.end():]
        for line in front.group(1).splitlines():
            key, _, value = line.partition(":")
            key, value = key.strip(), value.strip()
            if key == "topics":
                meta["topics"] = [t.strip().strip('"') for t in value.strip("[]").split(",") if t.strip()]
            elif key in ("generated", "fetched", "matched", "new_count"):
                meta[key] = value

    entries: list[dict] = []
    # Split on "## <section>" headings; part 0 is the preamble.
    parts = re.split(r"^##\s+(.+?)\s*$", body, flags=re.MULTILINE)
    for index in range(1, len(parts) - 1, 2):
        heading, content = parts[index], parts[index + 1]
        tier = next(
            (t for title, t in TIER_SECTIONS.items() if heading.startswith(title)),
            None,
        )
        if tier is None:
            continue  # e.g. "## 数据源状态"
        for block in re.split(r"^(?=###\s+\d+\.\s)", content, flags=re.MULTILINE):
            match = _ENTRY_RE.match(block)
            if not match:
                continue
            entry = _parse_entry(block, match)
            entry["tier"] = tier
            entry["tierLabel"] = TIER_TITLES[tier]
            entry["reportDate"] = day
            entries.append(entry)
    return meta, entries


def _parse_entry(block: str, match: re.Match) -> dict:
    title, url = _clean(match.group(1)), match.group(2).strip()

    doi = ""
    doi_match = re.search(r"\*\*DOI\*\*：\s*\[(.+?)\]", block)
    if doi_match:
        doi = doi_match.group(1).strip()

    # "**匹配**：空气污染（标题：no2）、遥感（标题：tropomi） ｜ **得分**：27"
    # One article routinely hits several keyword groups at once, and the report
    # joins them with "、". It belongs to *every* one of them - reading only the
    # first label silently drops about a quarter of the classification.
    raw_match = _field(block, "匹配")
    pairs = re.findall(r"([^、（）]+?)（(标题|摘要)：(.+?)）", raw_match)
    topics = [label.strip() for label, _, _ in pairs if label.strip()]
    if not topics and raw_match:
        topics = [raw_match.strip()]
    where = "、".join(f"{field}：{term}" for _, field, term in pairs)

    score_text = _field(block, "得分")
    score = int(score_text) if score_text.isdigit() else None

    authors_raw = re.search(r"\*\*作者\*\*：\s*(.+?)\s*$", block, re.MULTILINE)
    authors_text = _clean(authors_raw.group(1)) if authors_raw else ""
    more = authors_text.endswith("等")
    authors = [a.strip() for a in re.sub(r"\s*等\s*$", "", authors_text).split(",") if a.strip()]

    abstract = " ".join(q.strip() for q in re.findall(r"^>\s?(.+)$", block, re.MULTILINE)).strip()

    journal = _field(block, "期刊")
    return {
        "title": title,
        "url": url,
        "doi": doi,
        "journal": journal,
        "jabbr": JOURNAL_ABBR.get(journal, journal or "—"),
        "pubdate": _field(block, "日期"),
        # ``topic`` is the primary label (the first group that hit, i.e. config
        # order) and drives the cover chip and the section grouping; ``topics``
        # carries every match and drives filtering.
        "topic": topics[0] if topics else "其他",
        "topics": topics or ["其他"],
        "matchTerm": pairs[0][2].strip() if pairs else "",
        "matchWhere": where,
        "score": score,
        "authors": authors,
        "authorMore": more,
        "abstract": _clean(abstract),
    }


def collect(reports_dir: Path, topic_labels: list[str] | None = None
            ) -> tuple[list[dict], list[dict], list[str]]:
    """Parse every report into ``(articles, reports, topics)``.

    Reports are walked newest first and an article is kept only on its first
    sighting, so a paper that shows up in several days' reports appears once,
    with the freshest metadata.

    ``topic_labels`` is ``config.json``'s keyword order and wins outright. That
    matters when a topic has just been added: no report on disk mentions it yet,
    but it still has to get a tab, otherwise the category is invisible until the
    next report happens to contain it. Labels seen only in the reports are
    appended afterwards, so a report from an older config still renders.
    """
    # rglob, not glob: a day is now one top-level file plus one file per
    # discipline subdirectory. Sorting by stem keeps days in order regardless
    # of which folder a file sits in.
    paths = sorted(reports_dir.rglob("*.md"), key=lambda p: p.stem, reverse=True)
    by_day: dict[str, dict] = {}
    articles: list[dict] = []
    topics: list[str] = [t for t in (topic_labels or []) if t]
    seen: set[str] = set()

    for path in paths:
        meta, entries = parse_report(path)
        # Several files share a date now, so the day's row accumulates instead
        # of being appended once per file.
        day = by_day.setdefault(meta["date"], {"date": meta["date"], "generated": "", "count": 0})
        day["count"] += len(entries)
        day["generated"] = max(day["generated"], meta.get("generated", "") or "")
        for label in meta.get("topics", []):
            if label and label not in topics:
                topics.append(label)
        for entry in entries:
            key = (entry["doi"] or entry["title"]).lower()
            if key in seen:
                continue
            seen.add(key)
            for label in entry["topics"]:
                if label != "其他" and label not in topics:
                    topics.append(label)
            entry["tkey"] = THEME_BY_TOPIC.get(entry["topic"], "other")
            articles.append(entry)

    # Sort here as well as in the browser: the ids below are array positions,
    # so they must be handed out *after* the final order is fixed.
    articles.sort(key=lambda a: (a["score"] or 0, _sort_date(a["pubdate"])), reverse=True)
    for index, entry in enumerate(articles):
        entry["id"] = index

    reports = sorted(by_day.values(), key=lambda r: r["date"], reverse=True)
    return articles, reports, topics


def build_html(articles: list[dict], reports: list[dict], topics: list[str],
               *, project: str, generated_at: str,
               keyword_groups: list[dict] | None = None,
               repo: str = "pure-wang/article",
               workflow: str = "daily.yml") -> str:
    """Render the self-contained page."""
    payload = json.dumps(
        {
            "articles": articles,
            "reports": reports,
            "topics": topics,
            "themeKeys": list(THEME_KEYS),
            "topicTheme": THEME_BY_TOPIC,
            "generated": generated_at,
            # 增量：手动添加用 —— 关键词组（浏览器里预览分类）与仓库信息（提交落盘）
            "kw": keyword_groups or [],
            "repo": repo,
            "workflow": workflow,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    # Keep the payload safe to sit inside a <script> element.
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")

    return (
        _TEMPLATE
        .replace("/*__DATA__*/", payload)
        .replace("__PROJECT__", html.escape(project))
        .replace("__GEN__", generated_at)
        .replace("__TOTAL__", str(len(articles)))
    )


def load_keyword_groups(config_path: Path | str = "config.json") -> tuple[list[str], list[dict]]:
    """Read ``config.json`` for ``(topic labels, keyword groups)``.

    Both the CLI and ``run.py`` build the page through here, so a caller that
    only passes ``topic_labels`` still gets the keyword groups embedded for the
    in-page "手动添加" classifier. Missing or broken config is not fatal.
    """
    candidates = [Path(config_path), Path(__file__).resolve().parent.parent / "config.json"]
    for path in candidates:
        try:
            if not path.is_file():
                continue
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        labels = [k.get("label", "") for k in cfg.get("keywords", [])]
        groups = [
            {
                "label": k.get("label", ""),
                "weight": int(k.get("weight", 3)),
                "terms": [t for t in (k.get("terms") or [k.get("term", "")]) if t and t.strip()],
            }
            for k in cfg.get("keywords", [])
            if k.get("label") or k.get("term")
        ]
        return labels, groups
    return [], []


def write_cards(reports_dir: Path, out_path: Path, *, project: str = "文献日报",
                topic_labels: list[str] | None = None, log=None,
                keyword_groups: list[dict] | None = None,
                repo: str = "pure-wang/article",
                workflow: str = "daily.yml") -> Path | None:
    """Build the page from ``reports_dir`` into ``out_path``.

    Returns the written path, or ``None`` when there is nothing to publish.
    """
    if keyword_groups is None or topic_labels is None:
        cfg_labels, cfg_groups = load_keyword_groups()
        if keyword_groups is None:
            keyword_groups = cfg_groups
        if topic_labels is None:
            topic_labels = cfg_labels
    articles, reports, topics = collect(Path(reports_dir), topic_labels)
    if not articles:
        if log:
            log.info("cards page skipped: no reports found in %s", reports_dir)
        return None

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    out_path.write_text(
        build_html(articles, reports, topics, project=project, generated_at=generated_at,
                   keyword_groups=keyword_groups, repo=repo, workflow=workflow),
        encoding="utf-8",
        newline="\n",
    )
    if log:
        log.info("cards page written: %s (%d articles from %d reports)",
                 out_path, len(articles), len(reports))
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the static card-wall page from reports/")
    parser.add_argument("--reports", default="reports", help="directory holding the Markdown reports")
    parser.add_argument("--out", default="docs/index.html", help="output HTML path")
    parser.add_argument("--project", default="文献日报", help="project name shown in the header")
    parser.add_argument("--config", default="config.json",
                        help="config.json, read only to get the keyword label order")
    parser.add_argument("--repo", default="pure-wang/article",
                        help="owner/name of the GitHub repo the add button writes to")
    parser.add_argument("--workflow", default="daily.yml",
                        help="workflow file the add button triggers after committing")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    topic_labels: list[str] = []
    keyword_groups: list[dict] = []
    cfg_path = Path(args.config)
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            topic_labels = [k.get("label", "") for k in cfg.get("keywords", [])]
            # 增量：把关键词组原样带给页面，手动添加时可离线预览分类。
            keyword_groups = [
                {
                    "label": k.get("label", ""),
                    "weight": int(k.get("weight", 3)),
                    "terms": [t for t in (k.get("terms") or [k.get("term", "")]) if t and t.strip()],
                }
                for k in cfg.get("keywords", [])
                if k.get("label") or k.get("term")
            ]
        except (OSError, json.JSONDecodeError) as exc:
            print(f"警告：{cfg_path} 读取失败（{exc}），改用日报里的标签顺序。")
    else:
        topic_labels, keyword_groups = load_keyword_groups(cfg_path)

    written = write_cards(Path(args.reports), Path(args.out),
                          project=args.project, topic_labels=topic_labels,
                          keyword_groups=keyword_groups,
                          repo=args.repo, workflow=args.workflow)
    if written is None:
        print(f"没有在 {args.reports} 找到任何日报，未生成页面。")
        return 1
    print(f"已生成 {written}")
    return 0


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>__PROJECT__ · 文献卡片墙</title>
<style>
:root{
  --bg:#f1f2f4; --card:#fff; --ink:#1c1c1e; --ink2:#63636d; --ink3:#9d9da8;
  --line:#eeeef2; --red:#ff2442; --radius:16px;
  --sh:0 1px 2px rgba(16,16,24,.05), 0 6px 20px rgba(16,16,24,.06);
  --sh-hover:0 2px 6px rgba(16,16,24,.07), 0 14px 34px rgba(16,16,24,.12);
  --air-a:#ffb27a; --air-b:#ff5f7e;
  --rs-a:#63b3ff;  --rs-b:#3a5ce0;
  --chem-a:#c3a0ff; --chem-b:#7048e8;
  --bgc-a:#6ee0a8;  --bgc-b:#0b9e73;
  --ghg-a:#f7c95c;  --ghg-b:#b87300;
  --wx-a:#ff8494;   --wx-b:#c1121f;
  --gis-a:#5ed6c4;  --gis-b:#0d8f9e;
  --other-a:#c9ccd4; --other-b:#8b90a0;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  background:var(--bg); color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility; padding-bottom:56px;
}
a{color:inherit;text-decoration:none}
svg.ic{display:block;flex:none}

.topbar{
  position:sticky; top:0; z-index:60; background:rgba(241,242,244,.84);
  backdrop-filter:saturate(180%) blur(16px); -webkit-backdrop-filter:saturate(180%) blur(16px);
  border-bottom:1px solid rgba(0,0,0,.05);
}
.topbar-in{max-width:1560px;margin:0 auto;padding:14px 22px 0}
.brand{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.logo{
  width:30px;height:30px;border-radius:9px;flex:none;
  background:linear-gradient(135deg,var(--red),#ff7a59);
  display:grid;place-items:center;color:#fff;font-weight:800;font-size:15px;
  box-shadow:0 3px 10px rgba(255,36,66,.32);
}
.brand h1{font-size:17px;margin:0;font-weight:800;letter-spacing:.2px}
.brand .sub{font-size:12.5px;color:var(--ink3);margin-left:2px}
.brand .spacer{flex:1}
.kpi{display:flex;gap:6px;flex-wrap:wrap}
.kpi span{font-size:12px;color:var(--ink2);background:#fff;border:1px solid var(--line);padding:3px 9px;border-radius:999px;white-space:nowrap}
.kpi b{color:var(--red);font-weight:700}

.controls{max-width:1560px;margin:0 auto;padding:12px 22px;display:flex;flex-direction:column;gap:10px}
.row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.tabs{display:flex;gap:8px;overflow-x:auto;padding-bottom:2px;scrollbar-width:none}
.tabs::-webkit-scrollbar{display:none}
.tab{
  border:1px solid var(--line);background:#fff;color:var(--ink2);
  font-size:13.5px;font-weight:600;padding:7px 14px;border-radius:999px;cursor:pointer;
  white-space:nowrap;transition:.16s;display:flex;align-items:center;gap:7px;font-family:inherit;
}
.tab svg{width:15px;height:15px;stroke-width:1.9}
.tab:hover{border-color:#dcdce4;transform:translateY(-1px)}
.tab .n{font-size:11.5px;color:var(--ink3);font-weight:600}
.tab.on{background:var(--ink);border-color:var(--ink);color:#fff}
.tab.on .n{color:rgba(255,255,255,.66)}
.tab.on svg{stroke-width:2}
.search{
  flex:1;min-width:180px;max-width:340px;border:1px solid var(--line);background:#fff;border-radius:999px;
  padding:8px 15px;font-size:13.5px;outline:none;font-family:inherit;color:var(--ink);transition:.16s;
}
.search:focus{border-color:#ffc2cc;box-shadow:0 0 0 3px rgba(255,36,66,.09)}
.search::placeholder{color:var(--ink3)}
.seg{display:flex;background:#fff;border:1px solid var(--line);border-radius:999px;padding:2px}
.seg button{border:0;background:transparent;font-size:12.5px;padding:5px 12px;border-radius:999px;cursor:pointer;color:var(--ink2);font-family:inherit;transition:.16s}
.seg button.on{background:var(--ink);color:#fff;font-weight:600}
.count{font-size:12.5px;color:var(--ink3);margin-left:auto;white-space:nowrap}

.grouphead{max-width:1560px;margin:24px auto 0;padding:0 22px;display:flex;align-items:center;gap:10px}
.grouphead h2{font-size:15.5px;margin:0;font-weight:800;display:flex;align-items:center;gap:8px}
.grouphead h2 svg{width:18px;height:18px;stroke-width:1.9}
.grouphead .bar{flex:1;height:1px;background:linear-gradient(90deg,var(--line),transparent)}
.grouphead .gn{font-size:12.5px;color:var(--ink3);font-weight:600}
.g-air h2{color:#e8455f} .g-rs h2{color:#3a5ce0} .g-chem h2{color:#7048e8} .g-bgc h2{color:#0b9e73} .g-ghg h2{color:#b87300} .g-wx h2{color:#c1121f} .g-gis h2{color:#0d8f9e}

.wall{max-width:1560px;margin:0 auto;padding:14px 22px 0;columns:4;column-gap:16px}
@media(max-width:1380px){.wall{columns:3}}
@media(max-width:1000px){.wall{columns:2}}
@media(max-width:620px){
  .wall{columns:1}
  .topbar-in,.controls,.wall,.grouphead{padding-left:14px;padding-right:14px}
  .kpi{display:none}
}
.card{
  break-inside:avoid; -webkit-column-break-inside:avoid; page-break-inside:avoid;
  background:var(--card);border-radius:var(--radius);overflow:hidden;
  box-shadow:var(--sh);margin:0 0 16px;cursor:pointer;
  transition:transform .18s cubic-bezier(.2,.8,.2,1),box-shadow .18s;
  display:inline-block;width:100%;vertical-align:top;
}
.card:hover{transform:translateY(-3px);box-shadow:var(--sh-hover)}

.cover{position:relative;height:108px;overflow:hidden}
.cover::after{
  content:"";position:absolute;inset:0;pointer-events:none;
  background-image:radial-gradient(rgba(255,255,255,.3) 1px,transparent 1px);
  background-size:14px 14px;opacity:.45;
}
.t-air .cover{background:linear-gradient(135deg,var(--air-a),var(--air-b))}
.t-rs  .cover{background:linear-gradient(135deg,var(--rs-a),var(--rs-b))}
.t-chem .cover{background:linear-gradient(135deg,var(--chem-a),var(--chem-b))}
.t-bgc .cover{background:linear-gradient(135deg,var(--bgc-a),var(--bgc-b))}
.t-ghg .cover{background:linear-gradient(135deg,var(--ghg-a),var(--ghg-b))}
.t-wx .cover{background:linear-gradient(135deg,var(--wx-a),var(--wx-b))}
.t-gis .cover{background:linear-gradient(135deg,var(--gis-a),var(--gis-b))}
.t-other .cover{background:linear-gradient(135deg,var(--other-a),var(--other-b))}
.cover .icon{position:absolute;right:12px;top:50%;transform:translateY(-50%);color:#fff;opacity:.28;width:76px;height:76px;z-index:1}
.cover .icon svg{width:100%;height:100%;stroke-width:1.25}
.cover .jr{
  position:absolute;left:12px;bottom:11px;right:60px;color:#fff;font-size:13px;font-weight:800;
  letter-spacing:.3px;text-shadow:0 1px 6px rgba(0,0,0,.25);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;z-index:2;
}
.cover .tp{
  position:absolute;left:12px;top:10px;background:rgba(255,255,255,.95);color:#2a2a30;
  font-size:11px;font-weight:700;padding:3px 9px;border-radius:999px;z-index:2;
  box-shadow:0 1px 4px rgba(0,0,0,.1);
}
.cover .sc{
  position:absolute;right:12px;top:10px;background:rgba(0,0,0,.28);color:#fff;font-size:11px;
  font-weight:700;padding:3px 9px;border-radius:999px;z-index:2;backdrop-filter:blur(4px);
  font-variant-numeric:tabular-nums;
}
.tier{position:absolute;left:0;bottom:0;height:3px;width:100%;z-index:2}
.tier.must_read{background:#ff2442}
.tier.worth_reading{background:#ffab2e}
.tier.other{background:rgba(255,255,255,.6)}

.body{padding:12px 14px 11px}
.title{font-size:14.5px;font-weight:700;line-height:1.42;margin:0 0 7px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.excerpt{font-size:12.5px;line-height:1.62;color:var(--ink2);margin:0;display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}
.excerpt.none{color:var(--ink3);font-style:italic;font-size:12px;-webkit-line-clamp:2}
.tagrow{display:flex;gap:6px;flex-wrap:wrap;margin-top:9px}
.tag{font-size:11px;padding:2px 8px;border-radius:6px;background:#f4f5f7;color:var(--ink2);font-weight:600;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tag.must_read{background:#ffe9ed;color:#e01b39}
.tag.worth_reading{background:#fff4e3;color:#c67a00}
.tag.other{background:#f0f1f4;color:#7c8090}
.tag.match{background:#eef4ff;color:#3a5ce0}
.tag.cross{background:#e6f7f5;color:#0d8f9e}

.foot{display:flex;align-items:center;gap:8px;padding:9px 14px;border-top:1px solid var(--line);background:#fcfcfd}
.av{width:22px;height:22px;border-radius:50%;flex:none;color:#fff;display:grid;place-items:center;font-size:11px;font-weight:800}
.t-air .av{background:linear-gradient(135deg,var(--air-a),var(--air-b))}
.t-rs .av{background:linear-gradient(135deg,var(--rs-a),var(--rs-b))}
.t-chem .av{background:linear-gradient(135deg,var(--chem-a),var(--chem-b))}
.t-bgc .av{background:linear-gradient(135deg,var(--bgc-a),var(--bgc-b))}
.t-ghg .av{background:linear-gradient(135deg,var(--ghg-a),var(--ghg-b))}
.t-wx .av{background:linear-gradient(135deg,var(--wx-a),var(--wx-b))}
.t-gis .av{background:linear-gradient(135deg,var(--gis-a),var(--gis-b))}
.t-other .av{background:linear-gradient(135deg,var(--other-a),var(--other-b))}
.au{font-size:12px;color:var(--ink2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0}
.dt{font-size:11.5px;color:var(--ink3);white-space:nowrap;font-variant-numeric:tabular-nums}

.empty{max-width:1560px;margin:70px auto;text-align:center;color:var(--ink3);font-size:14px}
.empty svg{width:44px;height:44px;margin:0 auto 12px;stroke-width:1.5;opacity:.5;display:block}

.mask{position:fixed;inset:0;background:rgba(20,20,26,.5);z-index:100;display:none;align-items:center;justify-content:center;padding:24px;backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px)}
.mask.on{display:flex;animation:fade .16s ease}
@keyframes fade{from{opacity:0}to{opacity:1}}
.modal{background:#fff;border-radius:20px;max-width:760px;width:100%;max-height:86vh;overflow:auto;box-shadow:0 24px 70px rgba(0,0,0,.3);animation:pop .2s cubic-bezier(.2,.9,.3,1.2)}
@keyframes pop{from{opacity:0;transform:translateY(14px) scale(.98)}to{opacity:1;transform:none}}
.mhead{position:relative;padding:18px 22px 16px;color:#fff;overflow:hidden}
.mhead::after{content:"";position:absolute;inset:0;pointer-events:none;background-image:radial-gradient(rgba(255,255,255,.26) 1px,transparent 1px);background-size:16px 16px;opacity:.4}
.mhead .mj{font-size:12.5px;font-weight:700;opacity:.95;position:relative;z-index:2;letter-spacing:.3px}
.mhead h3{font-size:18px;line-height:1.42;margin:8px 0 0;position:relative;z-index:2;font-weight:800;text-shadow:0 1px 8px rgba(0,0,0,.18);padding-right:26px}
.mmeta{display:flex;gap:8px;flex-wrap:wrap;position:relative;z-index:2;margin-top:11px}
.mmeta span{background:rgba(255,255,255,.95);color:#2a2a30;font-size:11.5px;font-weight:700;padding:3px 10px;border-radius:999px}
.mclose{position:absolute;right:14px;top:14px;z-index:3;border:0;cursor:pointer;width:30px;height:30px;border-radius:50%;background:rgba(0,0,0,.22);color:#fff;font-size:16px;line-height:1;display:grid;place-items:center;font-family:inherit}
.mclose:hover{background:rgba(0,0,0,.36)}
.mbody{padding:18px 22px 22px}
.sec-t{font-size:11.5px;font-weight:800;color:var(--ink3);letter-spacing:.8px;margin:0 0 8px}
.abs{font-size:14px;line-height:1.78;color:#33333a;margin:0 0 20px;white-space:pre-wrap}
.authors{font-size:13.5px;line-height:1.7;color:var(--ink2);margin:0 0 18px}
.kv{font-size:12.5px;color:var(--ink2);line-height:1.9;margin:0 0 18px}
.kv code{background:#f4f5f7;padding:2px 7px;border-radius:5px;font-size:12px;color:#3a3a44}
.linkrow{display:flex;gap:9px;flex-wrap:wrap}
.btn{display:inline-flex;align-items:center;gap:6px;font-size:13.5px;font-weight:700;padding:9px 17px;border-radius:999px;border:1px solid var(--line);background:#fff;color:var(--ink);cursor:pointer;font-family:inherit;transition:.16s}
.btn:hover{border-color:#d8d8e0;transform:translateY(-1px)}
.btn.pri{background:var(--red);border-color:var(--red);color:#fff;box-shadow:0 3px 12px rgba(255,36,66,.28)}
.btn.pri:hover{background:#f01a38}

/* ---- 增量：个人批注 & 已读标记 ---- */
.noterow{margin-top:9px}
.note{width:100%;min-height:52px;max-height:220px;border:1px solid var(--line);border-radius:10px;background:#fafafa;padding:7px 9px;font-size:12px;line-height:1.55;font-family:inherit;color:var(--ink);resize:vertical;outline:none;transition:border-color .15s,box-shadow .15s;cursor:text}
.note:focus{border-color:#ffc2cc;box-shadow:0 0 0 3px rgba(255,36,66,.08);background:#fff}
.note::placeholder{color:var(--ink3)}
.notebtns{display:flex;gap:6px;margin-top:6px}
.readbtn{border:1px solid #ffd5db;background:#fff;color:#e01b39;font-size:11.5px;font-weight:700;padding:5px 11px;border-radius:999px;cursor:pointer;font-family:inherit;transition:.15s}
.readbtn:hover{background:#ff2442;border-color:#ff2442;color:#fff}
.unbtn{border:1px solid var(--line);background:#fff;color:var(--ink2);font-size:11.5px;font-weight:600;padding:5px 11px;border-radius:999px;cursor:pointer;font-family:inherit;transition:.15s}
.unbtn:hover{border-color:#d8d8e0;transform:translateY(-1px)}
.tag.isread{background:#e6f7f0;color:#0b9e73}
.card.isread{box-shadow:0 0 0 2px rgba(11,158,115,.35) inset,var(--sh)}
.mnote{width:100%;min-height:96px;border:1px solid var(--line);border-radius:10px;background:#fafafa;padding:10px 12px;font-size:13px;line-height:1.6;font-family:inherit;color:var(--ink);resize:vertical;outline:none;cursor:text}
.mnote:focus{border-color:#ffc2cc;box-shadow:0 0 0 3px rgba(255,36,66,.08);background:#fff}

/* ---- 增量：手动添加文献 ---- */
.addbtn{border:1px solid #ffd5db;background:#fff;color:#e01b39;font-size:12.5px;font-weight:700;padding:5px 12px;border-radius:999px;cursor:pointer;font-family:inherit;transition:.16s;white-space:nowrap}
.addbtn:hover{background:var(--red);border-color:var(--red);color:#fff}
.addform .fld{display:flex;flex-direction:column;gap:5px;margin-bottom:11px;min-width:0}
.addform .fld label{font-size:11px;font-weight:800;color:var(--ink3);letter-spacing:.7px}
.addform .fld input,.addform .fld select,.addform .fld textarea{
  width:100%;border:1px solid var(--line);border-radius:10px;background:#fafafa;padding:8px 11px;
  font-size:13px;font-family:inherit;color:var(--ink);outline:none;transition:border-color .15s,box-shadow .15s;cursor:text;
}
.addform .fld select{cursor:pointer}
.addform .fld input:focus,.addform .fld select:focus,.addform .fld textarea:focus{border-color:#ffc2cc;box-shadow:0 0 0 3px rgba(255,36,66,.08);background:#fff}
.addform .row2{display:flex;gap:11px}
.addform .row2 > .fld{flex:1}
.hint{font-size:12px;color:var(--ink2);line-height:1.68;margin:0 0 12px}
.hint b{color:var(--red)}
.kwprev{background:#f8f8fb;border:1px solid var(--line);border-radius:10px;padding:9px 12px;font-size:12.5px;line-height:1.72;color:var(--ink2);margin:0 0 12px}
.kwprev b{color:#e01b39}
.bm{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin:0 0 4px}
.bm a.btn{cursor:grab;text-decoration:none}
.status{font-size:12.5px;line-height:1.65;margin-top:11px;color:var(--ink2)}
.status.err{color:#d21f3c} .status.ok{color:#0b9e73;font-weight:600}
.mdprev{display:none;background:#f8f8fb;border:1px solid var(--line);border-radius:10px;padding:10px 12px;font-size:11.5px;line-height:1.6;white-space:pre-wrap;word-break:break-word;max-height:220px;overflow:auto;margin-top:11px;color:#3a3a44}

</style>
</head>
<body>

<div class="topbar">
  <div class="topbar-in">
    <div class="brand">
      <div class="logo">J</div>
      <h1>__PROJECT__ · 文献卡片墙</h1>
      <span class="sub">journal-alert · 日报可视化</span>
      <div class="spacer"></div>
      <button class="addbtn" id="addopen" type="button">＋ 添加文献</button>
      <div class="kpi">
        <span>总文献 <b>__TOTAL__</b></span>
        <span>已读 <b id="kpi-read">0</b></span>
        <span>数据源 <b id="kpi-days">—</b> 天日报</span>
        <span>更新 <b>__GEN__</b></span>
      </div>
    </div>
  </div>
  <div class="controls">
    <div class="tabs" id="tabs"></div>
    <div class="row">
      <input class="search" id="q" type="search" placeholder="搜索标题 / 摘要 / 作者 / 期刊…">
      <div class="seg" id="seg">
        <button data-t="all" class="on" type="button">全部等级</button>
        <button data-t="must_read" type="button">必读</button>
        <button data-t="worth_reading" type="button">值得一读</button>
        <button data-t="other" type="button">其他相关</button>
      </div>
      <div class="seg" id="sortseg">
        <button data-s="score" class="on" type="button">按得分</button>
        <button data-s="date" type="button">按日期</button>
        <button data-s="journal" type="button">按期刊</button>
      </div>
      <div class="count" id="count"></div>
    </div>
  </div>
</div>

<div id="wall-wrap"></div>

<div class="mask" id="mask">
  <div class="modal" id="modal" onclick="event.stopPropagation()"></div>
</div>

<script type="application/json" id="DATA">/*__DATA__*/</script>
<script>
(function(){
  var D = JSON.parse(document.getElementById('DATA').textContent);
  var A = D.articles;
  var TOPICS = D.topics;

  /* Inline SVG rather than emoji: emoji depend on a colour-emoji font being
     installed, and the missing-glyph box is worse than no icon at all. */
  var P = {
    all:   '<rect x="3" y="3" width="7.5" height="7.5" rx="2"/><rect x="13.5" y="3" width="7.5" height="7.5" rx="2"/><rect x="3" y="13.5" width="7.5" height="7.5" rx="2"/><rect x="13.5" y="13.5" width="7.5" height="7.5" rx="2"/>',
    air:   '<path d="M3 7.5h9.6a2.6 2.6 0 1 0-2.6-2.6"/><path d="M3 12h13.2a2.6 2.6 0 1 1-2.6 2.6"/><path d="M3 16.5h6.8"/>',
    rs:    '<circle cx="12" cy="12" r="8.6"/><ellipse cx="12" cy="12" rx="3.7" ry="8.6"/><path d="M3.4 12h17.2"/>',
    chem:  '<path d="M9.5 2.8h5"/><path d="M10.6 2.8v5.8L6.3 16.9a2 2 0 0 0 1.8 3h7.8a2 2 0 0 0 1.8-3L15.4 8.6V2.8"/><path d="M7.9 13.6h8.2"/>',
    bgc:   '<path d="M20.6 3.4C20.6 12 15.7 16.1 9.5 16.1H4.1C4.1 7.5 9 3.4 15.2 3.4h5.4z"/><path d="M4.1 20.6c2.2-5.8 6.1-9.1 11.3-10.5"/>',
    ghg:   '<path d="M4.6 20v-6.2"/><path d="M2.2 15.6 4.6 13.2l2.4 2.4"/><path d="M12 20V9.4"/><path d="M9.6 11.8 12 9.4l2.4 2.4"/><path d="M19.4 20v-3.4"/><path d="M17 18.4l2.4-2.4 2.4 2.4"/>',
    wx:    '<path d="M13.2 2.6 4.4 13.8h6.2l-1 7.6 8.8-11.2h-6.2l1-7.6z"/>',
    gis:   '<path d="M9 3.4 3.5 5.8v14.8L9 18.2l6 2.4 5.5-2.4V3.4L15 5.8 9 3.4z"/><path d="M9 3.4v14.8"/><path d="M15 5.8v14.8"/>',
    other: '<path d="M14 3H7.6A2 2 0 0 0 5.6 5v14a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2V7.6L14 3z"/><path d="M14 3v4.6h4.6"/>',
    search:'<circle cx="11" cy="11" r="7"/><path d="M20.2 20.2l-4.3-4.3"/>',
    read:  '<circle cx="12" cy="12" r="9"/><path d="M8.4 12.3l2.4 2.4 4.8-5"/>'
  };
  function svg(k, cls){
    return '<svg class="'+(cls||'ic')+'" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
         + 'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'+(P[k]||P.other)+'</svg>';
  }

  var state = { topic:'all', tier:'all', sort:'score', q:'' };

  /* ---- 增量：个人批注 & 已读标记（localStorage 持久化，换页面/刷新不丢）----
     以 DOI（无 DOI 用标题）为键，与文章数组顺序无关，日报更新后仍对得上。 */
  var RKEY = 'jalert-wall-notes-v1';
  var R = {};
  try { R = JSON.parse(localStorage.getItem(RKEY)) || {}; } catch(e) { R = {}; }
  function saveR(){ try { localStorage.setItem(RKEY, JSON.stringify(R)); } catch(e) {} }
  function akey(a){ return (a.doi || ('t:' + a.title)).toLowerCase(); }
  function isRead(a){ var r = R[akey(a)]; return !!(r && r.read); }
  function noteOf(a){ var r = R[akey(a)]; return (r && r.note) || ''; }
  function setNote(a, text){
    var k = akey(a);
    if (!R[k]) R[k] = { read: false, note: '', ts: 0 };
    R[k].note = text;
    R[k].ts = Date.now();
    saveR();
  }
  function setRead(a, on){
    var k = akey(a);
    if (!R[k]) R[k] = { read: false, note: noteOf(a), ts: 0 };
    R[k].read = on;
    R[k].ts = Date.now();
    saveR();
    updateReadBadge();
  }
  function readCount(){ return A.filter(isRead).length; }
  function updateReadBadge(){
    var n = readCount();
    if (readTabN) readTabN.textContent = n;
    var kpi = document.getElementById('kpi-read');
    if (kpi) kpi.textContent = n;
  }

  /* An article can hit several keyword groups at once. a.topics holds every
     match (used for filtering); a.topic is the primary one (used for the cover
     chip and the section grouping, so nothing is listed twice). */
  var THEME = D.topicTheme || {};
  function topicsOf(a){ return a.topics && a.topics.length ? a.topics : [a.topic]; }

  var tabs = document.getElementById('tabs');
  var C = {all:A.length};
  TOPICS.forEach(function(t){ C[t]=0; });
  A.forEach(function(a){
    topicsOf(a).forEach(function(t){ if(C[t]!==undefined) C[t]++; });
  });

  var tabDefs = [{k:'all',label:'全部',icon:'all'}].concat(
    TOPICS.map(function(t){ return {k:t,label:t,icon:THEME[t]||'other'}; }),
    [{k:'__read__',label:'已读',icon:'read'}]
  );
  var readTabN = null;
  tabDefs.forEach(function(d){
    var b = document.createElement('button');
    b.className = 'tab' + (d.k==='all'?' on':'');
    b.type = 'button';
    var n = d.k==='all' ? C.all : (C[d.k]||0);
    b.innerHTML = svg(d.icon) + '<span>'+d.label+'</span><span class="n">'+n+'</span>';
    if(d.k==='__read__'){
      readTabN = b.querySelector('.n');
      readTabN.textContent = 0;
    }
    b.onclick = function(){
      state.topic = d.k;
      [].forEach.call(tabs.children, function(x){ x.classList.toggle('on', x===b); });
      render();
    };
    tabs.appendChild(b);
  });

  document.getElementById('seg').onclick = function(e){
    var b = e.target.closest('button'); if(!b) return;
    state.tier = b.dataset.t;
    [].forEach.call(this.children, function(x){ x.classList.toggle('on', x===b); });
    render();
  };
  document.getElementById('sortseg').onclick = function(e){
    var b = e.target.closest('button'); if(!b) return;
    state.sort = b.dataset.s;
    [].forEach.call(this.children, function(x){ x.classList.toggle('on', x===b); });
    render();
  };
  var qt;
  document.getElementById('q').oninput = function(e){
    clearTimeout(qt);
    var v = e.target.value.trim().toLowerCase();
    qt = setTimeout(function(){ state.q = v; render(); }, 130);
  };

  function esc(s){
    return String(s==null?'':s).replace(/[&<>"']/g, function(c){
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
  }
  function trunc(s,n){ s=s||''; return s.length>n ? s.slice(0,n)+'…' : s; }
  function initial(name){
    if(!name) return '?';
    var m = /[A-Za-z]/.exec(name);
    return m ? m[0].toUpperCase() : name.slice(0,1);
  }
  function isFuture(d){ return (d||'').indexOf('（在线预发表）') >= 0; }
  function dateOf(a){ return (a.pubdate||a.reportDate||'').replace('（在线预发表）','').slice(0,10); }
  document.getElementById('kpi-days').textContent = D.reports.length;

  function select(){
    var q = state.q;
    var out = A.filter(function(a){
      /* 增量：已读标签页只看已读；其余视图一律隐藏已读（已读卡片只出现在已读区） */
      if(state.topic==='__read__'){ if(!isRead(a)) return false; }
      else if(isRead(a)) return false;
      if(state.topic!=='all' && state.topic!=='__read__' && topicsOf(a).indexOf(state.topic)<0) return false;
      if(state.tier!=='all' && a.tier!==state.tier) return false;
      if(q){
        var hay = (a.title+' '+a.abstract+' '+a.authors.join(' ')+' '+a.journal+' '+a.doi).toLowerCase();
        if(hay.indexOf(q)<0) return false;
      }
      return true;
    });
    if(state.sort==='date'){
      out.sort(function(x,y){ return dateOf(y).localeCompare(dateOf(x)) || ((y.score||0)-(x.score||0)); });
    } else if(state.sort==='journal'){
      out.sort(function(x,y){ return x.journal.localeCompare(y.journal) || ((y.score||0)-(x.score||0)); });
    } else {
      out.sort(function(x,y){ return ((y.score||0)-(x.score||0)) || dateOf(y).localeCompare(dateOf(x)); });
    }
    return out;
  }

  function cardHTML(a){
    var tags = '<span class="tag '+a.tier+'">'+esc(a.tierLabel)+'</span>'
             + '<span class="tag">'+esc(a.jabbr)+'</span>';
    if(a.score!=null) tags += '<span class="tag">得分 '+a.score+'</span>';
    if(a.matchTerm) tags += '<span class="tag match" title="命中位置：'+esc(a.matchWhere)+'">命中 '+esc(a.matchTerm)+'</span>';
    topicsOf(a).filter(function(t){ return t!==a.topic; }).slice(0,2).forEach(function(t){
      tags += '<span class="tag cross">也属 '+esc(t)+'</span>';
    });

    var ab = a.abstract
      ? '<p class="excerpt">'+esc(trunc(a.abstract,200))+'</p>'
      : '<p class="excerpt none">日报未收录摘要'+(a.matchTerm?'（命中词 '+esc(a.matchTerm)+'）':'')+' · 点击看原文</p>';

    var au = esc(a.authors[0] || '佚名') + (a.authors.length>1 ? ' 等 '+(a.authors.length-1)+' 位' : '');

    /* 增量：已读徽标 + 批注框 + 已读/恢复按钮（样式与原有 tag/button 体系一致） */
    var read = isRead(a);
    var tags2 = read ? '<span class="tag isread">✓ 已读</span>' : '';
    var noteVal = esc(noteOf(a));
    var noterow =
      '<div class="noterow">'
      + '<textarea class="note" data-id="'+a.id+'" placeholder="个人批注 & 总结笔记…（自动保存）">'+noteVal+'</textarea>'
      + '<div class="notebtns">'
      +   (read
            ? '<button class="unbtn" type="button" data-id="'+a.id+'">恢复未读</button>'
            : '<button class="readbtn" type="button" data-id="'+a.id+'">✓ 标记为已读</button>')
      + '</div>'
      + '</div>';

    return '<article class="card t-'+a.tkey+(read?' isread':'')+'" data-id="'+a.id+'" tabindex="0">'
      + '<div class="cover">'
      +   '<div class="tp">'+esc(a.topic)+'</div>'
      +   (a.score!=null ? '<div class="sc">★ '+a.score+'</div>' : '')
      +   '<div class="icon">'+svg(a.tkey)+'</div>'
      +   '<div class="jr">'+esc(a.jabbr)+'</div>'
      +   '<div class="tier '+a.tier+'"></div>'
      + '</div>'
      + '<div class="body">'
      +   '<h3 class="title">'+esc(a.title)+'</h3>'
      +   ab
      +   '<div class="tagrow">'+tags2+tags+'</div>'
      +   noterow
      + '</div>'
      + '<div class="foot">'
      +   '<div class="av">'+esc(initial(a.authors[0]))+'</div>'
      +   '<div class="au">'+au+'</div>'
      +   '<div class="dt">'+esc(dateOf(a))+(isFuture(a.pubdate)?' · 预发表':'')+'</div>'
      + '</div>'
      + '</article>';
  }

  var wrap = document.getElementById('wall-wrap');
  var countEl = document.getElementById('count');
  var TIER_NAME = {must_read:'必读', worth_reading:'值得一读', other:'其他相关'};

  function render(){
    var list = select();
    var readTotal = readCount();
    var scope = (state.topic==='all' ? '全部学科' : (state.topic==='__read__' ? '已读文献' : state.topic))
              + (state.tier==='all' ? '' : ' · '+TIER_NAME[state.tier]);
    countEl.textContent = scope + ' ｜ ' + list.length
      + (state.topic==='__read__' ? ' / '+readTotal+' 篇' : ' / ' + A.length + ' 篇');

    if(!list.length){
      wrap.innerHTML = '<div class="empty">'+svg('search')
        +(state.topic==='__read__' && !readTotal ? '还没有标记已读的文献。在卡片上点「✓ 标记为已读」，读过就会收进这里。' : '没有匹配的文献，换个关键词或学科试试')
        +'</div>';
      return;
    }

    var html = '';
    if(state.topic==='all' && state.tier==='all' && !state.q && state.sort==='score'){
      TOPICS.forEach(function(t){
        var g = list.filter(function(a){ return a.topic===t; });
        if(!g.length) return;
        var key = THEME[t] || 'other';
        var related = list.filter(function(a){ return topicsOf(a).indexOf(t)>=0; }).length - g.length;
        html += '<div class="grouphead'+(key!=='other'?' g-'+key:'')+'"><h2>'+svg(key)+t+'</h2>'
              + '<span class="gn">'+g.length+' 篇'
              + (related>0 ? ' · 另有 '+related+' 篇跨学科命中' : '')+'</span><div class="bar"></div></div>'
              + '<div class="wall">'+g.map(cardHTML).join('')+'</div>';
      });
      var rest = list.filter(function(a){ return TOPICS.indexOf(a.topic)<0; });
      if(rest.length){
        html += '<div class="grouphead"><h2>'+svg('other')+'其他</h2>'
              + '<span class="gn">'+rest.length+' 篇</span><div class="bar"></div></div>'
              + '<div class="wall">'+rest.map(cardHTML).join('')+'</div>';
      }
      /* 增量：默认视图底部追加「已读文献」区（list 已被 select 排除已读，这里单独取） */
      var rd = A.filter(function(a){
        if(!isRead(a)) return false;
        if(state.tier!=='all' && a.tier!==state.tier) return false;
        if(state.q){
          var hay = (a.title+' '+a.abstract+' '+a.authors.join(' ')+' '+a.journal+' '+a.doi).toLowerCase();
          if(hay.indexOf(state.q)<0) return false;
        }
        return true;
      });
      if(rd.length){
        html += '<div class="grouphead"><h2>'+svg('read')+'已读文献</h2>'
              + '<span class="gn">'+rd.length+' 篇</span><div class="bar"></div></div>'
              + '<div class="wall">'+rd.map(cardHTML).join('')+'</div>';
      }
    } else {
      html = '<div class="wall">'+list.map(cardHTML).join('')+'</div>';
    }
    wrap.innerHTML = html;
  }

  var mask = document.getElementById('mask'), modal = document.getElementById('modal');
  function openModal(id){
    var a = A[id]; if(!a) return;
    var authors = a.authors.length ? a.authors.join('、') + (a.authorMore?' 等':'') : '未收录作者信息';
    modal.innerHTML =
      '<div class="mhead" style="background:linear-gradient(135deg,var(--'+a.tkey+'-a),var(--'+a.tkey+'-b))">'
      + '<button class="mclose" id="mclose" type="button">✕</button>'
      + '<div class="mj">'+esc(topicsOf(a).join(' / '))+' · '+esc(a.journal)+'</div>'
      + '<h3>'+esc(a.title)+'</h3>'
      + '<div class="mmeta">'
      +   '<span>'+esc(a.tierLabel)+'</span>'
      +   (a.score!=null?'<span>得分 '+a.score+'</span>':'')
      +   (a.pubdate?'<span>'+esc(a.pubdate)+'</span>':'')
      +   '<span>日报 '+esc(a.reportDate)+'</span>'
      + '</div></div>'
      + '<div class="mbody">'
      +   '<div class="sec-t">摘要</div>'
      +   (a.abstract
            ? '<p class="abs">'+esc(a.abstract)+'</p>'
            : '<p class="abs" style="color:#9d9da8">该条日报未收录摘要。</p>')
      +   '<div class="sec-t">作者</div><p class="authors">'+esc(authors)+'</p>'
      +   (a.doi ? '<div class="kv">DOI：<code>'+esc(a.doi)+'</code></div>' : '')
      +   (a.matchWhere ? '<div class="kv">关键词命中：'+esc(a.matchWhere)+'</div>' : '')
      +   '<div class="sec-t">个人批注 & 总结</div>'
      +   '<textarea class="mnote" id="mnote" placeholder="在这里写你的批注与总结…（自动保存）">'+esc(noteOf(a))+'</textarea>'
      +   '<div class="linkrow">'
      +     '<a class="btn pri" href="'+esc(a.url)+'" target="_blank" rel="noopener">打开原文 ↗</a>'
      +     (a.doi?'<a class="btn" href="https://doi.org/'+esc(a.doi)+'" target="_blank" rel="noopener">DOI 链接</a>':'')
      +     '<button class="btn" id="mcopy" type="button">复制 DOI</button>'
      +     '<button class="btn" id="mtoggle" type="button">'+(isRead(a)?'恢复未读':'✓ 标记为已读')+'</button>'
      +   '</div>'
      + '</div>';
    mask.classList.add('on');
    document.body.style.overflow = 'hidden';
    var cb = document.getElementById('mclose'); if(cb) cb.onclick = closeModal;
    var cp = document.getElementById('mcopy');
    if(cp) cp.onclick = function(){
      var txt = a.doi || a.url;
      var done = function(){ cp.textContent='已复制 ✓'; setTimeout(function(){ cp.textContent='复制 DOI'; },1400); };
      if(navigator.clipboard && navigator.clipboard.writeText){
        navigator.clipboard.writeText(txt).then(done, function(){ cp.textContent = txt; });
      } else { cp.textContent = txt; }
    };
    /* 增量：弹窗内批注（同一存储，卡片和弹窗实时一致） */
    var mn = document.getElementById('mnote');
    if(mn) mn.oninput = function(){ setNote(a, mn.value); };
    var mt = document.getElementById('mtoggle');
    if(mt) mt.onclick = function(){
      setRead(a, !isRead(a));
      closeModal();
      render();
    };
  }
  function closeModal(){ mask.classList.remove('on'); document.body.style.overflow=''; }
  mask.onclick = closeModal;
  document.addEventListener('keydown', function(e){ if(e.key==='Escape') closeModal(); });

  /* ---- 增量：手动添加文献 ----
     书签只把期刊页上的元数据塞进 #add=（不携带任何令牌），真正的写入在本页完成。
     令牌只存在本浏览器的 localStorage，不进书签、不进 URL。
     条目写成和日报一样的 Markdown，落到 reports/manual/<日期>.md，解析器无需改动。 */
  var PATKEY = 'jalert-gh-pat-v1';
  var MANUAL_TOPIC = '杂文献';
  function getPat(){ try { return localStorage.getItem(PATKEY) || ''; } catch(e){ return ''; } }
  function savePat(v){ try { localStorage.setItem(PATKEY, v); } catch(e){} }
  function todayISO(){
    var d = new Date(), p = function(n){ return (n < 10 ? '0' : '') + n; };
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
  }
  function kwEscape(s){ return String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
  function kwRe(term){ return new RegExp('(?<![a-z0-9])' + kwEscape(term) + '(?![a-z0-9])', 'i'); }

  /* 与 jalert/score.py 同一套规则：标题命中 ×3、摘要命中 ×1，各组取最好的一条。 */
  function classify(title, abstract){
    title = title || ''; abstract = abstract || '';
    var hits = [];
    (D.kw || []).forEach(function(g){
      var terms = g.terms || [], hit = null, i;
      for (i = 0; i < terms.length && !hit; i++){
        if (kwRe(terms[i]).test(title)) hit = { label: g.label, term: terms[i], where: '标题', pts: (g.weight || 3) * 3 };
      }
      if (!hit) for (i = 0; i < terms.length; i++){
        if (kwRe(terms[i]).test(abstract)) { hit = { label: g.label, term: terms[i], where: '摘要', pts: (g.weight || 3) }; break; }
      }
      if (hit) hits.push(hit);
    });
    var score = 0;
    hits.forEach(function(h){ score += h.pts; });
    return {
      hits: hits,
      score: hits.length ? score : null,
      tier: score >= 12 ? 'must_read' : (score >= 6 ? 'worth_reading' : 'other'),
      label: hits.length ? hits[0].label : MANUAL_TOPIC
    };
  }
  function matchLineOf(hits){
    if (!hits.length) return MANUAL_TOPIC;
    return hits.map(function(h){
      return h.label + '（' + h.where + '：' + String(h.term).replace(/[、（）]/g, ' ') + '）';
    }).join('、');
  }
  function cleanTitle(t){ return String(t || '').replace(/[\[\]\r\n]+/g, ' ').replace(/\s+/g, ' ').trim(); }
  function buildMarkdown(p, n){
    var title = cleanTitle(p.title);
    /* 没有 DOI/链接时必须给一个可用的兜底链接：空括号会让解析器丢条目。 */
    var url = (p.url || '').trim() || (p.doi ? 'https://doi.org/' + p.doi
              : 'https://scholar.google.com/scholar?q=' + encodeURIComponent(title));
    url = url.replace(/\(/g, '%28').replace(/\)/g, '%29');
    var meta = [];
    if (p.journal) meta.push('**期刊**：' + String(p.journal).replace(/\s+/g, ' ').trim());
    meta.push('**日期**：' + (p.date || todayISO()));
    if (p.doi) meta.push('**DOI**：[' + p.doi + '](https://doi.org/' + p.doi + ')');
    var lines = ['## ' + p.tierTitle + '（1）', '', '### ' + n + '. [' + title + '](' + url + ')',
                 '- ' + meta.join(' ｜ ')];
    lines.push('- **匹配**：' + p.matchLine + (p.score != null ? ' ｜ **得分**：' + p.score : ''));
    if (p.authors && p.authors.length) lines.push('- **作者**：' + p.authors.join(', '));
    if (p.abstract) lines.push('', '> ' + String(p.abstract).replace(/\s+/g, ' ').trim());
    return lines.join('\n') + '\n';
  }
  function b64enc(s){
    var bytes = new TextEncoder().encode(s), bin = '';
    for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin);
  }
  function b64dec(b64){
    var bin = atob(String(b64 || '').replace(/\s/g, ''));
    var bytes = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new TextDecoder().decode(bytes);
  }
  function ghApi(path, opts){
    opts = opts || {};
    var headers = { 'Accept': 'application/vnd.github+json' };
    var pat = opts.pat || getPat();
    if (pat) headers['Authorization'] = 'Bearer ' + pat;
    if (opts.body) headers['Content-Type'] = 'application/json';
    opts.headers = headers;
    opts.pat = undefined;
    return fetch('https://api.github.com/repos/' + D.repo + path, opts);
  }
  function makeBookmarklet(){
    var target = location.origin + location.pathname + '#add=';
    var code = '(function(){'
      + 'var m=function(n){var e=document.querySelector(\'meta[name="\'+n+\'"]\')||document.querySelector(\'meta[property="\'+n+\'"]\');return e?e.content:\'\'};'
      + 'var ms=function(n){var o=[];document.querySelectorAll(\'meta[name="\'+n+\'"]\').forEach(function(e){o.push(e.content)});return o};'
      + 'var doi=m(\'citation_doi\')||m(\'dc.identifier\')||m(\'prism.doi\')||\'\';'
      + 'if(!doi){var t=(document.body.innerText||\'\').slice(0,8000);var r=t.match(/10\\.[0-9]{4,9}\\/[^\\s"<>]+/);doi=r?r[0]:\'\'}'
      + 'doi=doi.replace(/^https?:\\/\\/(dx\\.)?doi\\.org\\//i,\'\').replace(/[.,;]+$/,\'\');'
      + 'var d={title:m(\'citation_title\')||m(\'dc.title\')||document.title,'
      + 'authors:ms(\'citation_author\').concat(ms(\'dc.creator\')),'
      + 'journal:m(\'citation_journal_title\')||m(\'prism.publicationName\')||m(\'dc.source\'),'
      + 'date:m(\'citation_publication_date\')||m(\'citation_date\')||m(\'dc.date\'),'
      + 'doi:doi,url:location.href,'
      + 'abstract:m(\'citation_abstract\')||m(\'dc.description\')||m(\'description\')||\'\'};'
      + 'location.href=' + JSON.stringify(target) + '+encodeURIComponent(btoa(unescape(encodeURIComponent(JSON.stringify(d)))))'
      + '})()';
    return 'javascript:' + code;
  }

  function openAdd(pre){
    pre = pre || {};
    var topicsAll = TOPICS.slice();
    if (topicsAll.indexOf(MANUAL_TOPIC) < 0) topicsAll.push(MANUAL_TOPIC);
    var auto = classify(pre.title || '', pre.abstract || '');
    var topicHtml = '<option value="">自动判断</option>'
      + topicsAll.map(function(t){ return '<option value="' + esc(t) + '">' + esc(t) + '</option>'; }).join('');
    var tierHtml = '<option value="">自动判断</option>'
      + ['must_read', 'worth_reading', 'other'].map(function(k){
          return '<option value="' + k + '"' + (auto.tier === k && !auto.hits.length ? '' : '') + '>' + TIER_NAME[k] + '</option>';
        }).join('');
    var authText = (pre.authors || []).join(', ');
    var pat = getPat();

    modal.innerHTML =
      '<div class="mhead" style="background:linear-gradient(135deg,var(--other-a),var(--other-b))">'
      + '<button class="mclose" id="mclose" type="button">✕</button>'
      + '<div class="mj">手动添加 · 写入 reports/manual/</div>'
      + '<h3>添加到卡片墙</h3>'
      + '</div>'
      + '<div class="mbody addform">'
      +   '<p class="hint">在期刊/PubMed 页面点<b>书签</b>可自动填好下面字段；也可以直接手填。分类按本地关键词规则自动判断，可手动改；分不进任何组的归<b>杂文献</b>。提交后写入仓库并触发重建，约 1–2 分钟上线。</p>'
      +   '<div class="fld"><label>标题 *</label><input id="f-title" type="text" value="' + esc(pre.title || '') + '"></div>'
      +   '<div class="row2">'
      +     '<div class="fld"><label>期刊</label><input id="f-journal" type="text" value="' + esc(pre.journal || '') + '"></div>'
      +     '<div class="fld"><label>日期</label><input id="f-date" type="text" placeholder="' + todayISO() + '" value="' + esc((pre.date || '').slice(0, 10)) + '"></div>'
      +   '</div>'
      +   '<div class="row2">'
      +     '<div class="fld"><label>DOI</label><input id="f-doi" type="text" value="' + esc(pre.doi || '') + '"></div>'
      +     '<div class="fld"><label>原文链接</label><input id="f-url" type="text" value="' + esc(pre.url || '') + '"></div>'
      +   '</div>'
      +   '<div class="fld"><label>作者（英文逗号分隔）</label><input id="f-authors" type="text" value="' + esc(authText) + '"></div>'
      +   '<div class="fld"><label>摘要</label><textarea id="f-abstract" rows="4">' + esc(pre.abstract || '') + '</textarea></div>'
      +   '<div class="row2">'
      +     '<div class="fld"><label>分类</label><select id="f-topic">' + topicHtml + '</select></div>'
      +     '<div class="fld"><label>等级</label><select id="f-tier">' + tierHtml + '</select></div>'
      +   '</div>'
      +   '<div class="kwprev" id="f-prev"></div>'
      +   '<div class="fld"><label>GitHub 令牌（只存本浏览器）</label>'
      +     '<input id="f-pat" type="password" autocomplete="off" placeholder="github_pat_…" value="' + esc(pat) + '"></div>'
      +   '<div class="bm">'
      +     '<a class="btn" id="f-bm" href="#" title="拖到浏览器书签栏">📌 卡片墙书签（拖到书签栏）</a>'
      +     '<span class="hint" style="margin:0">装一次即可：在任意文献页点书签，自动抓 DOI/标题/摘要并回到这里。</span>'
      +   '</div>'
      +   '<div class="linkrow">'
      +     '<button class="btn pri" id="f-go" type="button">提交到卡片墙</button>'
      +     '<button class="btn" id="f-md" type="button">预览 Markdown</button>'
      +   '</div>'
      +   '<div class="status" id="f-status"></div>'
      +   '<pre class="mdprev" id="f-mdbox"></pre>'
      + '</div>';

    mask.classList.add('on');
    document.body.style.overflow = 'hidden';
    var cb0 = document.getElementById('mclose'); if(cb0) cb0.onclick = closeModal;
    var bm = document.getElementById('f-bm'); if(bm) bm.href = makeBookmarklet();

    function val(id){ var e = document.getElementById(id); return e ? e.value.trim() : ''; }
    function compute(){
      var title = val('f-title'), abstract = val('f-abstract');
      var auto2 = classify(title, abstract);
      var topicSel = val('f-topic'), tierSel = val('f-tier');
      var hits = topicSel ? auto2.hits.filter(function(h){ return h.label === topicSel; }) : auto2.hits;
      var score = null;
      if (topicSel) { if (hits.length) { score = 0; hits.forEach(function(h){ score += h.pts; }); } }
      else score = auto2.score;
      var tier = tierSel || auto2.tier;
      var label = topicSel || auto2.label;
      return {
        title: title, abstract: abstract,
        topic: label, hits: hits, score: score, tier: tier,
        tierTitle: TIER_NAME[tier],
        matchLine: hits.length ? (topicSel && !auto2.hits.some(function(h){ return h.label === topicSel; }) ? label : matchLineOf(hits)) : label,
        authors: val('f-authors').split(',').map(function(s){ return s.trim(); }).filter(Boolean),
        journal: val('f-journal'), date: val('f-date'), doi: val('f-doi'), url: val('f-url')
      };
    }
    function refresh(){
      var p = compute();
      var dup = A.filter(function(a){
        if (p.doi && a.doi) return a.doi.toLowerCase() === p.doi.toLowerCase();
        return p.title && a.title.toLowerCase() === p.title.toLowerCase();
      })[0];
      var lines = ['分类：<b>' + esc(p.topic) + '</b> ｜ 等级：<b>' + esc(p.tierTitle) + '</b>'
                   + (p.score != null ? ' ｜ 得分：<b>' + p.score + '</b>' : '')];
      if (p.hits.length) lines.push('命中：' + p.hits.map(function(h){ return esc(h.label) + '（' + h.where + '：' + esc(h.term) + '）'; }).join('、'));
      else lines.push(p.title ? '没有命中任何关键词组 → 归入「' + MANUAL_TOPIC + '」' : '填入标题后自动判断分类');
      if (dup) lines.push('⚠ 这篇已经在卡片墙里了（重复添加不会生效）');
      document.getElementById('f-prev').innerHTML = lines.join('<br>');
      return p;
    }
    function showMarkdown(){
      var p = refresh();
      var md = buildMarkdown(p, 1);
      var box = document.getElementById('f-mdbox');
      box.textContent = md;
      box.style.display = box.style.display === 'block' ? 'none' : 'block';
    }
    ['f-title', 'f-abstract', 'f-journal', 'f-doi', 'f-url', 'f-date', 'f-authors', 'f-topic', 'f-tier']
      .forEach(function(id){
        var el = document.getElementById(id);
        if (el) el.oninput = refresh, el.onchange = refresh;
      });
    document.getElementById('f-md').onclick = showMarkdown;
    document.getElementById('f-go').onclick = function(){
      var btn = this, st = document.getElementById('f-status');
      var p = refresh();
      if (!p.title) { st.className = 'status err'; st.textContent = '标题不能为空。'; return; }
      var pat = val('f-pat');
      if (!pat) { st.className = 'status err'; st.textContent = '请先填入 GitHub 令牌（fine-grained，仅本仓库 Contents 读写 + Actions 读写）。'; return; }
      savePat(pat);
      btn.disabled = true;
      st.className = 'status';
      st.textContent = '正在写入仓库…';
      submitEntry(p, pat).then(function(res){
        st.className = 'status ok';
        st.innerHTML = '已提交：commit <b>' + esc(res.commit) + '</b>（' + esc(p.topic) + ' · ' + esc(p.tierTitle) + '）'
          + (res.dispatched ? '，已触发重建，约 1–2 分钟后出现。' : '，请稍后手动刷新。');
        btn.textContent = '已提交 ✓';
      }).catch(function(err){
        st.className = 'status err';
        st.textContent = '提交失败：' + (err && err.message ? err.message : err);
        btn.disabled = false;
      });
    };
    refresh();
  }

  function submitEntry(p, pat){
    /* 归档目录按“提交日”而不是“发表日期”：日报行表示哪天整理过，
       文章本身的发表日期仍然写在条目的 **日期** 字段里。 */
    var path = '/contents/reports/manual/' + todayISO() + '.md';
    var body = '', sha = null;
    return ghApi(path + '?ref=main', { pat: pat }).then(function(g){
      if (g.status === 200) {
        return g.json().then(function(j){ sha = j.sha; body = b64dec(j.content); });
      }
      if (g.status !== 404) throw new Error('读取现有文件失败：HTTP ' + g.status);
    }).then(function(){
      var n = (body.match(/^###\s+\d+\./gm) || []).length + 1;
      var entry = buildMarkdown(p, n);
      var next = body ? body.replace(/\s*$/, '') + '\n\n' + entry : entry;
      var payload = {
        message: 'docs(manual): 手动添加文献「' + cleanTitle(p.title).slice(0, 50) + '」',
        content: b64enc(next),
        branch: 'main'
      };
      if (sha) payload.sha = sha;
      return ghApi(path, { method: 'PUT', body: JSON.stringify(payload), pat: pat });
    }).then(function(put){
      return put.json().then(function(j){
        if (put.status !== 200 && put.status !== 201) {
          throw new Error('写入失败：HTTP ' + put.status + (j && j.message ? ' ' + j.message : ''));
        }
        return j;
      });
    }).then(function(j){
      return ghApi('/actions/workflows/' + D.workflow + '/dispatches', {
        method: 'POST', body: JSON.stringify({ ref: 'main' }), pat: pat
      }).then(function(dis){
        return { commit: (j.commit && j.commit.sha || '').slice(0, 7), dispatched: dis.status === 204 };
      }, function(){
        return { commit: (j.commit && j.commit.sha || '').slice(0, 7), dispatched: false };
      });
    });
  }

  function readAddHash(){
    var h = location.hash || '';
    if (h.indexOf('#add=') !== 0) return null;
    try {
      var raw = decodeURIComponent(h.slice(5));
      var bin = atob(raw);
      var bytes = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      var obj = JSON.parse(new TextDecoder().decode(bytes));
      if (history.replaceState) history.replaceState(null, '', location.pathname + location.search);
      return obj;
    } catch (e) { return null; }
  }
  document.getElementById('addopen').onclick = function(){ openAdd({}); };

  wrap.addEventListener('click', function(e){
    /* 增量：批注框与按钮不触发详情弹窗 */
    if(e.target.closest('.note')) return;
    var rb = e.target.closest('.readbtn');
    if(rb){ var ca = A[+rb.dataset.id]; if(ca){ setRead(ca, true); render(); } return; }
    var ub = e.target.closest('.unbtn');
    if(ub){ var ua = A[+ub.dataset.id]; if(ua){ setRead(ua, false); render(); } return; }
    var c = e.target.closest('.card'); if(c) openModal(+c.dataset.id);
  });
  /* 增量：批注输入 → 自动保存（防抖 300ms） */
  var noteT;
  wrap.addEventListener('input', function(e){
    var t = e.target.closest('.note'); if(!t) return;
    var a = A[+t.dataset.id]; if(!a) return;
    clearTimeout(noteT);
    noteT = setTimeout(function(){ setNote(a, t.value); }, 300);
  });
  wrap.addEventListener('keydown', function(e){
    /* 增量：在批注框内 Enter 换行，不触发弹窗 */
    if(e.key==='Enter' && e.target.closest('.note')) return;
    if(e.key==='Enter'){ var c = e.target.closest('.card'); if(c) openModal(+c.dataset.id); }
  });

  updateReadBadge();
  render();
  /* 增量：书签带来的 #add= 载荷 → 直接打开添加表单 */
  var pendingAdd = readAddHash();
  if (pendingAdd) openAdd(pendingAdd);
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())

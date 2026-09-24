"""Render the final scan as SLOP_REPORT.md and a self-contained slop_report.html."""

import datetime
import json
import posixpath
from pathlib import Path

from schema import SEVERITIES
from workspace import SKILL_DIR

_HTML_TEMPLATE = SKILL_DIR / "assets" / "report_template.html"
_DATA_PLACEHOLDER = "/*__SLOP_DATA__*/null"


def build_report_data(run, manifest, scores, kept, dismissed, priorities, module_summaries, system, chief, coverage, usage,
                      fingerprint=None, duplication=None, flows=(), reach=None):
    """system: the primary tier-3 output; chief: the chief's output. Either may be {} if it failed.
    fingerprint, duplication, flows and reach are the deterministic fingerprint, clones.json, traced
    flows and the reachability brief."""
    languages = {}
    for entry in manifest.values():
        languages[entry["language"]] = languages.get(entry["language"], 0) + entry["loc"]
    by_id = {f["id"]: f for f in kept}
    return {
        "meta": {
            "root": run["root"],
            "name": Path(run["root"]).name,
            "scanned": run["created"],
            "generated": datetime.datetime.now().isoformat(timespec="seconds"),
            "languages": dict(sorted(languages.items(), key=lambda item: -item[1])),
            "history": run["history"],
        },
        "score": scores,
        "executive_summary": chief.get("executive_summary", ""),
        "architecture_summary": system.get("architecture_summary", ""),
        "themes": chief.get("themes") or system.get("themes", []),
        "priorities": [{**by_id[p["finding_id"]], "why": p["why"]} for p in priorities],
        "modules": module_summaries,
        "findings": kept,
        "dismissed": [
            {"id": f["id"], "title": f["title"], "where": _where(f["locations"][0]), "severity": f["severity"],
             "reason": f"merged into {f['merged_into']}" if f.get("merged_into") else f["review"]["rationale"],
             **({"appeal": f["appeal"]["rationale"]} if f.get("appeal") else {})}
            for f in dismissed
        ],
        "flows": list(flows),
        "fingerprint": fingerprint,
        "reach": reach,
        "duplication": {
            "parallel_trees": (duplication or {}).get("parallel_trees", [])[:15],
            "module_pairs": (duplication or {}).get("module_pairs", [])[:15],
            "largest_copies": (duplication or {}).get("pairs", [])[:20],
            "total_pairs": (duplication or {}).get("total_pairs", 0),
        },
        "appeals": {
            "restored": sum(1 for f in kept if f.get("appeal", {}).get("action") == "overturn"),
            "upheld": sum(1 for f in kept + dismissed if f.get("appeal", {}).get("action") == "uphold"),
        },
        "coverage": coverage,
        "usage": usage,
    }


def write_reports(data, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    markdown = out_dir / "SLOP_REPORT.md"
    html = out_dir / "slop_report.html"
    markdown.write_text(render_markdown(data), encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html.write_text(_HTML_TEMPLATE.read_text(encoding="utf-8").replace(_DATA_PLACEHOLDER, payload), encoding="utf-8")
    return markdown, html


def render_markdown(data):
    score, meta = data["score"], data["meta"]
    lines = [
        f"# Slop Scan: {meta['name']}",
        "",
        f"**Grade {score['grade']}** · slop index **{score['slop_index']}** per 1k LOC · "
        f"{score.get('duplication_pct', 0)}% copied code · "
        f"{score['files_scanned']} files · {score['total_loc']:,} LOC · scanned {meta['scanned'][:10]}",
        "",
    ]
    if score.get("grade_reasons"):
        lines += ["Grade set by: " + "; ".join(score["grade_reasons"]) + ".", ""]
    lines += [f"History: {_history_label(meta['history'])}", ""]
    gaps = [f"{tier} {c['done']}/{c['tasks']}" for tier, c in data["coverage"].items() if c["done"] < c["tasks"]]
    if gaps:
        lines += [f"> **Partial scan.** Some worker tasks failed ({', '.join(gaps)}); their areas are under-reported.", ""]

    if data["executive_summary"]:
        lines += ["## Summary", "", data["executive_summary"], ""]
    if data["priorities"]:
        lines += ["## Fix first", ""]
        lines += [
            f"{number}. **{p['title']}** (`{_where(p['locations'][0])}`, {p['severity']}, effort {p['effort']}): {p['why']}"
            for number, p in enumerate(data["priorities"], 1)
        ]
        lines.append("")
    if data["themes"]:
        lines += ["## Themes", ""]
        lines += [f"- **{theme['title']}.** {theme['description']}" for theme in data["themes"]]
        lines.append("")
    if data["architecture_summary"]:
        lines += ["## Architecture", "", data["architecture_summary"], ""]
    if data.get("flows"):
        lines += ["## Critical flows", ""]
        for flow in data["flows"]:
            lines += [f"### {flow['name']} ({flow['health']})", "", flow["summary"], "",
                      f"**Ownership:** {flow['ownership']}", ""]
            lines += [f"{number}. {step}" for number, step in enumerate(flow["steps"], 1)]
            lines.append("")
    if data.get("fingerprint"):
        lines += _markdown_fingerprint(data["fingerprint"])
    if data.get("duplication") and (data["duplication"]["parallel_trees"] or data["duplication"]["largest_copies"]):
        lines += _markdown_duplication(data["duplication"])
    if data.get("reach") and data["reach"]["analyzed"]["files"]:
        lines += _markdown_reach(data["reach"])

    lines += ["## Scorecard", "", "| Severity | Findings |", "|---|---:|"]
    lines += [f"| {severity} | {score['by_severity'][severity]} |" for severity in SEVERITIES]
    lines += ["", "| Category | Findings | Weight |", "|---|---:|---:|"]
    lines += [f"| {name} | {row['count']} | {row['weight']:.1f} |" for name, row in score["by_category"].items()]
    lines.append("")

    lines += ["## Hotspots", "", "| File | Weight | LOC | Used by | Commits | Density |", "|---|---:|---:|---:|---:|---:|"]
    lines += [f"| `{r['name']}` | {r['weight']} | {r['loc']} | {r['fan_in']} | {r['commits']} | {r['density']} |"
              for r in score["hotspot_files"][:15]]
    lines += ["", "| Module | Weight | LOC | Commits | Density |", "|---|---:|---:|---:|---:|"]
    lines += [f"| `{r['name']}` | {r['weight']} | {r['loc']} | {r['commits']} | {r['density']} |" for r in score["hotspot_modules"][:10]]
    lines.append("")

    lines += ["## Findings", ""]
    for severity in SEVERITIES:
        group = [f for f in data["findings"] if f["severity"] == severity]
        if group:
            lines += [f"### {severity.capitalize()} ({len(group)})", ""]
            for finding in group:
                lines += _markdown_finding(finding)

    if data["modules"]:
        lines += ["## Module health", "", "| Module | Health | Cohesion | Coupling | Summary |", "|---|---|---|---|---|"]
        lines += [
            f"| `{m['module']}` | {m['health']} | {m['cohesion']} | {m['coupling']} | {_cell(m['summary'])} |"
            for m in data["modules"]
        ]
        lines.append("")

    if data["dismissed"]:
        appeals = data.get("appeals") or {}
        note = f"; {appeals['restored']} more were restored on appeal and appear above" if appeals.get("restored") else ""
        lines += [f"<details><summary>{len(data['dismissed'])} findings dismissed by the editors as not worth fixing{note}</summary>", ""]
        lines += [f"- {d['title']} (`{d['where']}`): {d['reason']}" + (f" Appeal upheld: {d['appeal']}" if d.get("appeal") else "")
                  for d in data["dismissed"]]
        lines += ["", "</details>", ""]

    usage = data["usage"]
    if usage:
        lines += [f"<sub>{usage['sessions']} codex sessions · {usage['input_tokens']:,} input tokens "
                  f"({usage['cached_input_tokens']:,} cached) · {usage['output_tokens']:,} output tokens</sub>", ""]
    return "\n".join(lines)


def _markdown_finding(finding):
    primary = finding["locations"][0]
    status = _status(finding)
    status = f" · {status}" if status else ""
    block = [
        f"#### {finding['title']}",
        f"`{_where(primary)}` · `{finding['category']}/{finding['rule']}` · confidence {finding['confidence']} · "
        f"effort {finding['effort']}{status} · `{finding['id']}`",
        "",
        f"- **Evidence:** {finding['evidence']}",
        f"- **Impact:** {finding['impact']}",
        f"- **Fix:** {finding['recommendation']}",
    ]
    if finding.get("appeal", {}).get("action") == "overturn":
        block.append(f"- **Appeal:** {finding['appeal']['rationale']} (editor: {finding['review']['rationale']})")
    if len(finding["locations"]) > 1:
        block.append("- **Also at:** " + ", ".join(f"`{_where(loc)}`" for loc in finding["locations"][1:]))
    return block + [""]


def _status(finding):
    review, appeal = finding.get("review"), finding.get("appeal") or {}
    if appeal.get("action") == "overturn":
        return "restored on appeal" if review and review["action"] == "drop" else f"re-rated on appeal to {finding['severity']}"
    if review and review["action"] == "rerate":
        return f"re-rated from {finding['original_severity']}"
    return "reviewed" if review else ""


def _history_label(history):
    maturity = {"mature": "established"}.get(history["maturity"], history["maturity"])
    if maturity == "none":
        return "none (judged on structure alone)"
    if "total_commits" not in history:  # a run made before the history window existed
        return f"{maturity}, {history['commits']} commits over {history['span_days']} days"
    label = (f"{maturity}, {history['total_commits']:,} commits since {history['first_commit']}; "
             f"{history['window_commits']:,} in the last {history['window_days']} days touch the scanned files; "
             f"{history.get('hot_files', 0)} hot files, {history.get('new_files', 0)} files created in that window")
    if history.get("capped"):
        label += " (window capped at the newest commits)"
    return label + (" (co-change withheld: too young)" if maturity == "young" else "")


def _markdown_fingerprint(fingerprint):
    repo = fingerprint["repo"]
    labels = {s["key"]: s["label"] for s in fingerprint["signals"]}
    columns = [s["key"] for s in fingerprint["signals"] if s["key"] != "todo_markers"]
    lines = ["## Slop fingerprint", "",
             "Counted by the script, not judged by the workers, so pruning cannot hide it. Rates are per 1k LOC "
             "(copied: % of non-test LOC). **Bold** = at least twice the repository rate. "
             + " ".join(f"*{s['label']}*: {s['description']}." for s in fingerprint["signals"] if s["key"] in columns), "",
             "| Module | LOC | " + " | ".join(labels[c] for c in columns) + " | files ≥3k LOC |",
             "|---|---:|" + "---:|" * len(columns) + "---:|",
             "| **whole repository** | " + f"{repo['loc']:,} | " + " | ".join(str(repo[c]) for c in columns)
             + f" | {repo['giant_files']} |"]
    for row in fingerprint["modules"][:15]:
        cells = [f"**{row[c]}**" if c in row["flagged"] else str(row[c]) for c in columns]
        lines.append(f"| `{row['module']}` | {row['loc']:,} | " + " | ".join(cells) + f" | {row['giant_files']} |")
    giant = fingerprint["files"].get("giant") or []
    if giant:
        lines += ["", "Largest files: " + ", ".join(f"`{g['path']}` ({g['loc']:,})" for g in giant[:8])]
    return lines + [""]


def _markdown_duplication(duplication):
    lines = ["## Duplication", ""]
    if duplication["parallel_trees"]:
        lines += ["Sibling directories that look like two versions of one thing. Some are by design (one backend per "
                  "platform); \"changed together\" says whether they are maintained in lockstep.", "",
                  "| Tree | Tree | LOC | Same-named files | Copied tokens | Changed together |", "|---|---|---:|---:|---:|---:|"]
        for tree in duplication["parallel_trees"][:10]:
            together = tree.get("cochange")
            together = f"{together['both']} commits ({together['ratio']:.0%} of the quieter tree's)" if together else "n/a"
            lines.append(f"| `{tree['a']}` | `{tree['b']}` | {tree['loc_a']:,} / {tree['loc_b']:,} | "
                         f"{tree['shared_names']} | {tree['copied_tokens']:,} | {together} |")
        lines.append("")
    if duplication["largest_copies"]:
        lines += [f"Largest copied blocks ({duplication['total_pairs']} in total):", ""]
        lines += [f"- `{_where(p['a'])}` = `{_where(p['b'])}` ({p['tokens']:,} tokens)" for p in duplication["largest_copies"][:12]]
        lines.append("")
    return lines


REACH_LABELS = {
    "launched": "launched by checked-in configuration or a framework",
    "manual": "only from entry points nothing checked in starts",
    "tests": "only by tests",
    "none": "by nothing",
}
FAMILY_NAMES = {"python": "Python", "js": "JavaScript/TypeScript"}


def _markdown_reach(reach):
    totals = reach["totals"]
    lines = ["## Reachability", "",
             "What checked-in configuration actually runs (containers, compose, CI, Procfiles, Makefiles, package.json, "
             "pyproject, shell scripts, framework conventions), followed through imports. Counted by the script for "
             f"Python and JavaScript/TypeScript ({reach['analyzed']['loc']:,} non-test LOC), not judged by the workers. "
             "Docs don't count as launching anything. Dynamic loading the script can't see makes \"by nothing\" wrong, "
             "so treat it as a lead.", "",
             "| Reached | Files | LOC |", "|---|---:|---:|"]
    lines += [f"| {REACH_LABELS[kind]} | {totals[kind]['files']} | {totals[kind]['loc']:,} |" for kind in REACH_LABELS]
    lines.append("")
    if reach["launchers"]:
        shown = reach["launchers"][:10]
        lines.append("Launched by: " + "; ".join(f"`{l['config']}` → `{l['path']}` ({l['how']})" for l in shown)
                     + (f"; and {len(reach['launchers']) - len(shown)} more" if len(reach["launchers"]) > len(shown) else "")
                     + ".")
    if reach["conventions"]:
        lines.append("By convention: " + ", ".join(f"{name} ({count})" for name, count in reach["conventions"].items()) + ".")
    if reach["self_started"]:
        lines.append("No checked-in launcher for " + " or ".join(FAMILY_NAMES[f] for f in reach["self_started"])
                     + " code, so files that start themselves count as launched.")
    lines.append("")
    groups, scripts = _split_scripts(reach["manual"])
    if groups or scripts:
        lines += ["Reachable only from entry points nothing checked in starts:", ""]
        lines += [f"- {', '.join(f'`{r}`' for r in g['roots'])} reach {g['file_count']} files ({g['loc']:,} LOC): "
                  + ", ".join(f"`{p}`" for p in g["files"][:8]) + (" …" if g["file_count"] > 8 else "") for g in groups]
        if scripts:
            lines.append(f"- {len(scripts)} standalone scripts ({sum(g['loc'] for g in scripts):,} LOC): "
                         + ", ".join(f"`{g['roots'][0]}`" for g in scripts[:8]) + (" …" if len(scripts) > 8 else ""))
        lines.append("")
    for key, title in (("tests_only", "Used only by tests"), ("unreached", "Reached by nothing")):
        if reach[key]:
            lines += [f"{title}:", ""]
            lines += [f"- `{g['module']}` ({g['loc']:,} LOC): " + ", ".join(f"`{posixpath.basename(p)}`" for p in g["files"][:8])
                      + (" …" if g["file_count"] > 8 else "") for g in reach[key][:12]]
            lines.append("")
    return lines


def _split_scripts(groups):
    """Manual entry-point groups, and the one-file scripts among them (listed together)."""
    scripts = [g for g in groups if g["file_count"] == 1 and g["files"] == g["roots"]]
    return [g for g in groups if g not in scripts], scripts


def _where(location):
    start, end = location.get("start_line"), location.get("end_line")
    if start and end and end != start:
        return f"{location['path']}:{start}-{end}"
    return f"{location['path']}:{start}" if start else location["path"]


def _cell(text):
    return text.replace("|", "\\|").replace("\n", " ")

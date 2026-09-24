"""Assemble the self-contained prompt for one worker task.

Cheap models do best when everything they need is in front of them, so the prompt carries
the role, the rubric, the relevant part of the output contract, the task, and where it is
small enough the code itself with line numbers.
"""

import json
import re
from pathlib import Path

from gitlog import HOT_COMMITS
from workspace import SKILL_DIR

REFERENCES = SKILL_DIR / "references"
ROLE_FOR_TIER = {"tier1": "scout", "tier2": "architect", "tier3": "systems", "flows": "flow",
                 "editor": "editor", "appeal": "appeal", "chief": "chief"}
CONTRACT_SECTIONS = {
    "tier1": ("Lenses", "Finding", "Tier 1"),
    "tier2": ("Lenses", "Finding", "Tier 2"),
    "tier3": ("Lenses", "Finding", "Tier 3"),
    "flows": ("Finding", "Flow tracer"),
    "editor": ("Editor",),
    "appeal": ("Appeal",),
    "chief": ("Chief",),
}
EXCERPT_CONTEXT = 12  # lines shown around each finding for the editor
EXCERPT_MAX_LINES = 90
ENTRY_POINT_LINES = 60  # lines shown from each flow entry point's definition
MAX_REPAIR_ANSWER_CHARS = 60_000


def build(tier, task, root, history):
    root = Path(root)
    parts = [
        _read("workers", f"{ROLE_FOR_TIER[tier]}.md"),
        "# Rubric\n\n" + _read("taxonomy.md"),
        "# Output contract\n\n" + _contract(tier),
        "# Repository history\n\n" + history_note(history),
        "# Your task\n\n```json\n" + json.dumps(task, indent=1, ensure_ascii=False) + "\n```",
    ]
    if tier == "tier1":
        parts.append("# Code\n\n" + "\n\n".join(_file_listing(root, entry) for entry in task["files"]))
    elif tier in ("editor", "appeal"):
        parts.append("# Code at each finding\n\n" + "\n\n".join(_finding_excerpts(root, f) for f in task["findings"]))
    elif tier == "flows":
        excerpts = [_definition_excerpt(root, ref) for ref in task.get("definitions", [])]
        if excerpts:
            parts.append("# Entry points\n\n" + "\n\n".join(excerpts))
    parts.append("Reply with the JSON object only: no prose, no code fences.")
    return "\n\n".join(parts)


def history_note(history):
    """Tell the worker what the commit data means in this repository."""
    maturity = {"mature": "established"}.get(history.get("maturity"), history.get("maturity"))
    if maturity == "none":
        return ("No git history. Decide whether things belong together from reason-to-change and "
                "`structure`; prioritize by centrality (`fan_in`).")
    if "total_commits" not in history:  # a run made before the history window existed
        facts = f"{history['commits']} commits over {history['span_days']} days"
        window = "that history"
    else:
        window = f"the last {history['window_days']} days"
        facts = (f"{history['total_commits']:,} commits since {history['first_commit']}; "
                 f"{history['window_commits']:,} in {window} touch the scanned files, "
                 f"{history.get('hot_files', 0)} files have {HOT_COMMITS}+ commits in {window}")
    if maturity == "young":
        return (f"Young repository ({facts}). Too little history for `co_changes` (withheld). `commits` "
                "still shows where work happens, but everything is new: decide whether things belong "
                "together from reason-to-change and `structure`; prioritize by centrality (`fan_in`) and `commits`.")
    return (f"Established repository ({facts}). `commits` counts each file's commits in {window}: a file "
            "with many is hot, and structural mess there taxes every change. A file with few commits is "
            "stable code when `age_days` is null (it predates the window) or new code when `age_days` is "
            "small; new code in a central place is cheapest to fix now. Judge every file on its own "
            "numbers, never on the repository average. `co_changes` lists files that change in the same "
            "commits: a high ratio means they move together.")


def repair(prompt, answer, problems):
    listed = "\n".join(f"- {problem}" for problem in problems[:40])
    return (
        f"{prompt}\n\n# Your previous answer\n\n{answer[:MAX_REPAIR_ANSWER_CHARS]}\n\n"
        f"# Problems with it\n\n{listed}\n\n"
        "Fix these problems and reply with the complete corrected JSON object only."
    )


def _read(*parts):
    return REFERENCES.joinpath(*parts).read_text(encoding="utf-8").strip()


def _contract(tier):
    """Pick the `## ` sections of contracts.md whose titles start with the tier's section names."""
    sections = re.split(r"(?m)^## ", _read("contracts.md"))
    wanted = CONTRACT_SECTIONS[tier]
    return "\n\n".join("## " + s.strip() for s in sections[1:] if s.startswith(wanted))


def _file_listing(root, entry):
    lines = _lines(root / entry["path"])
    segment = entry.get("segment")
    start, end = (segment["start_line"], segment["end_line"]) if segment else (1, len(lines))
    header = f"## {entry['path']}"
    if segment:
        header += f" (segment {segment['part']} of {segment['parts']}: lines {start}-{end} of {len(lines)})"
    return f"{header}\n\n```\n{_numbered(lines, start, end)}\n```"


def _finding_excerpts(root, finding):
    blocks = [f"## {finding['id']}"]
    for location in finding["locations"][:2]:
        path = root / location["path"]
        start = location.get("start_line")
        if not start or not path.is_file():
            blocks.append(f"`{location['path']}`: module-level; inspect it with the shell if needed.")
            continue
        end = location.get("end_line") or start
        lines = _lines(path)
        low = max(1, start - EXCERPT_CONTEXT)
        high = min(len(lines), end + EXCERPT_CONTEXT, low + EXCERPT_MAX_LINES - 1)
        blocks.append(f"`{location['path']}` lines {low}-{high}:\n```\n{_numbered(lines, low, high)}\n```")
    return "\n\n".join(blocks)


def _definition_excerpt(root, ref):
    """The first lines of an entry point's definition, for the flow tracer."""
    lines = _lines(Path(root) / ref["path"])
    start = ref["line"]
    end = min(len(lines), start + ENTRY_POINT_LINES - 1)
    return f"`{ref['path']}` lines {start}-{end} (`{ref['symbol']}`):\n```\n{_numbered(lines, start, end)}\n```"


def _lines(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _numbered(lines, start, end):
    return "\n".join(f"{number:>5}| {lines[number - 1]}" for number in range(start, min(end, len(lines)) + 1))

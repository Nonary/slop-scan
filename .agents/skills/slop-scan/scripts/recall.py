"""Check a finished scan against problems you already know about.

The scan's grade and finding count say nothing about what it missed. Recall does: list the
problems a careful reviewer would expect (or that you planted in a copy of the code), and this
reports which ones the scan kept, which it found but dismissed, and which it never raised.

expected.json is a list of {"path", "lines": [start, end], "category", "keywords", "note"}.
Only `path` is required; it may be a directory, to accept any finding under it. `category` (one
or a list) and `keywords` (any of them must appear in the finding's title, evidence or fix) keep
a broad expectation from being satisfied by an unrelated finding in the same place.
"""


def check(findings, expected):
    """findings: the run's findings.json ({"findings", "dismissed"}). Returns rows and a summary."""
    rows = []
    for number, item in enumerate(expected, 1):
        kept = [f for f in findings["findings"] if _matches(f, item)]
        dismissed = [f for f in findings["dismissed"] if _matches(f, item)]
        if kept:
            status, hits = "kept", kept
        elif dismissed:
            status, hits = "dismissed", dismissed
        else:
            status, hits = "missed", []
        rows.append({
            "expected": item.get("note") or f"expectation {number}",
            "where": _where(item),
            "status": status,
            "matches": [
                {"id": f["id"], "severity": f["severity"], "title": f["title"],
                 **({"reason": f.get("merged_into") and f"merged into {f['merged_into']}" or f.get("review", {}).get("rationale", "")}
                    if status == "dismissed" else {})}
                for f in hits[:3]
            ],
        })
    total = len(rows) or 1
    counts = {status: sum(1 for r in rows if r["status"] == status) for status in ("kept", "dismissed", "missed")}
    return {"summary": {**counts, "expected": len(rows), "recall": round(counts["kept"] / total, 2),
                        "found_at_all": round((counts["kept"] + counts["dismissed"]) / total, 2)},
            "rows": rows}


def render(result):
    summary = result["summary"]
    lines = [f"Recall {summary['recall']:.0%}: {summary['kept']} of {summary['expected']} expected problems kept; "
             f"{summary['dismissed']} found but dismissed; {summary['missed']} never raised.", ""]
    for row in result["rows"]:
        lines.append(f"[{row['status'].upper():9}] {row['expected']} ({row['where']})")
        for match in row["matches"]:
            reason = f" -- dismissed: {match['reason']}" if match.get("reason") else ""
            lines.append(f"             {match['id']} {match['severity']}: {match['title']}{reason}")
    return "\n".join(lines)


def _matches(finding, item):
    categories = item.get("category")
    if categories and finding["category"] not in ([categories] if isinstance(categories, str) else categories):
        return False
    keywords = item.get("keywords")
    if keywords:
        text = " ".join(finding.get(k, "") for k in ("title", "evidence", "recommendation")).lower()
        if not any(word.lower() in text for word in ([keywords] if isinstance(keywords, str) else keywords)):
            return False
    want = item["path"].rstrip("/")
    lines = item.get("lines")
    for location in finding["locations"]:
        path = location["path"]
        if path == want:
            start, end = location.get("start_line"), location.get("end_line") or location.get("start_line")
            if not lines or start is None or (start <= lines[-1] and lines[0] <= end):
                return True
        elif path.startswith(want + "/") or want.startswith(path + "/") and not lines:
            return True  # a directory expectation, or a module-level finding covering the file
    return False


def _where(item):
    lines = item.get("lines")
    return f"{item['path']}:{lines[0]}-{lines[-1]}" if lines else item["path"]

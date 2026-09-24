"""The slop fingerprint: deterministic per-module rates of the habits careless or generated code
leaves behind. Workers cannot prune it, so it is the report's check on their judgment.

Every signal is a count from metrics.py or clones.py. Rates are per 1,000 lines of code, and a
module is flagged on a signal when its rate is at least twice the repository's. There are no
absolute "good" thresholds here: they would need calibration across many codebases.
"""

from collections import defaultdict

GIANT_FILE_LOC = 3000
MIN_MODULE_LOC = 500  # smaller modules make rates jumpy
FLAG_FACTOR = 2.0
# (key in the output, manifest counter, column name, description).
# Rates are per 1k LOC, except duplication (percent of non-test LOC).
SIGNALS = (
    ("duplication_pct", "duplicated_lines", "copied %", "lines inside a copied block, % of non-test LOC"),
    ("swallowed_catches", "swallowed_catches", "swallowed", "broad catches whose body only logs, returns a default, or is empty"),
    ("catch_all", "catch_all", "broad catches", "catch (...), catch (Exception), bare except and similar"),
    ("log_calls", "log_calls", "log calls", "logging calls"),
    ("hedges", "hedges", "hedges", "mentions of fallback, best-effort, workaround, just in case"),
    ("narrating_comments", "narrating_comments", "narrating", "comments that restate the code line below them"),
    ("long_functions", "long_functions", "fns >100 lines", "functions longer than 100 lines"),
    ("todo_markers", "todo_markers", "TODOs", "TODO, FIXME, HACK and XXX markers"),
)


def summarize(manifest, limit=20):
    """{"repo": {...}, "modules": [...], "files": {...}, "signals": [...]} for reports and prompts."""
    totals = defaultdict(float)
    modules = defaultdict(lambda: defaultdict(float))
    for entry in manifest.values():
        for bucket in (totals, modules[entry["module"]]):
            bucket["loc"] += entry["loc"]
            bucket["files"] += 1
            bucket["giant_files"] += entry["loc"] >= GIANT_FILE_LOC
            if not entry.get("is_test"):
                bucket["code_loc"] += entry["loc"]
                bucket["code_duplicated_lines"] += entry.get("duplicated_lines", 0)
            for _, counter, _, _ in SIGNALS:
                bucket[counter] += entry.get(counter, 0)

    repo = _rates(totals)
    rows = []
    for name, bucket in modules.items():
        if bucket["loc"] < MIN_MODULE_LOC:
            continue
        rates = _rates(bucket)
        flagged = [key for key, *_ in SIGNALS if repo[key] > 0 and rates[key] >= FLAG_FACTOR * repo[key]]
        rows.append({"module": name, **rates, "flagged": flagged})
    rows.sort(key=lambda r: (-len(r["flagged"]), -r["loc"]))

    files = {
        counter: [
            {"path": path, "count": entry.get(counter, 0), "loc": entry["loc"]}
            for path, entry in sorted(manifest.items(), key=lambda item: -item[1].get(counter, 0))[:8]
            if entry.get(counter, 0)
        ]
        for counter in ("duplicated_lines", "swallowed_catches", "log_calls", "hedges", "narrating_comments")
    }
    files["giant"] = [
        {"path": path, "count": entry["loc"], "loc": entry["loc"]}
        for path, entry in sorted(manifest.items(), key=lambda item: -item[1]["loc"])
        if entry["loc"] >= GIANT_FILE_LOC
    ][:15]
    return {
        "repo": repo,
        "modules": rows[:limit],
        "files": files,
        "signals": [{"key": key, "label": label, "description": description} for key, _, label, description in SIGNALS],
    }


def _rates(bucket):
    kloc = max(bucket["loc"], 1) / 1000
    rates = {
        "loc": int(bucket["loc"]),
        "files": int(bucket["files"]),
        "giant_files": int(bucket["giant_files"]),
        # Duplication is measured on non-test code: copied test setup is a lower-stakes habit.
        "duplication_pct": round(100 * bucket["code_duplicated_lines"] / max(bucket["code_loc"], 1), 1),
    }
    for key, counter, _, _ in SIGNALS:
        if key != "duplication_pct":
            rates[key] = round(bucket[counter] / kloc, 2)
    return rates

"""Merge worker findings into one ranked list: stable ids, de-duplication, verdicts, scores."""

import hashlib
from collections import defaultdict

from schema import SEVERITIES
from workspace import read_json

SEVERITY_WEIGHT = {"critical": 10.0, "high": 5.0, "medium": 2.0, "low": 0.5}
CONFIDENCE_FACTOR = {"high": 1.0, "medium": 0.7, "low": 0.4}
TIER_ORDER = ("tier1", "tier2", "tier3", "flows")
GRADES = ("A", "B", "C", "D", "F")
# The grade is the worst of three gates, so no single number can hide a serious problem.
# 1. Slop index (weighted findings per 1,000 lines of code). Scouts report a handful of findings
#    per ~1,200 lines and editors prune them, so the index rarely passes 5; the bands are set so
#    that every letter is reachable. They are provisional: calibrate them with `recall` runs.
GRADE_BANDS = ((1, "A"), (2, "B"), (4, "C"), (8, "D"))
# 2. Serious findings cap the grade however large the codebase: (severity, at least n, best grade).
SEVERITY_GATES = (("critical", 3, "D"), ("critical", 1, "C"), ("high", 20, "D"), ("high", 10, "C"), ("high", 5, "B"))
# 3. Copied code, as a percentage of non-test lines: (at least pct, best grade).
DUPLICATION_GATES = ((20, "D"), (10, "C"), (5, "B"))


def collect(workspace, valid_outputs):
    """valid_outputs: {tier: [output dicts]} for tiers 1-3. Returns merged findings, ids assigned."""
    raw = []
    for tier in TIER_ORDER:
        for output in valid_outputs.get(tier, []):
            for finding in output["findings"]:
                raw.append({**finding, "sources": [f"{tier}:{output['task_id']}"]})
    merged = _dedupe(raw)
    used = set()
    for finding in merged:
        finding_id = _stable_id(finding)
        while finding_id in used:
            finding_id += "x"
        used.add(finding_id)
        finding["id"] = finding_id
    return sorted(merged, key=rank_key)


def apply_editor(findings, editor_outputs):
    """Apply editor decisions. Returns (kept, dismissed); dismissed findings carry a reason.
    Findings no editor saw (out of scope, or a failed batch) are kept unreviewed."""
    decisions = {d["finding_id"]: d for output in editor_outputs for d in output["decisions"]}
    by_id = {f["id"]: dict(f) for f in findings}
    dismissed = []
    for finding_id, finding in by_id.items():
        decision = decisions.get(finding_id)
        if not decision:
            continue
        finding["review"] = {"action": decision["action"], "rationale": decision["rationale"]}
        if decision["action"] == "rerate":
            finding["original_severity"] = finding["severity"]
            finding["severity"] = decision["severity"]
        elif decision["action"] == "drop":
            dismissed.append(finding)
    gone = {f["id"] for f in dismissed}
    for finding_id, decision in decisions.items():
        if decision["action"] == "merge" and finding_id in by_id:
            target = _merge_target(by_id, decisions, decision["merge_into"])
            if target is None or target["id"] == finding_id or target["id"] in gone:
                continue  # the target is gone or the merge loops back: keep this one
            _absorb(target, by_id[finding_id])
            dismissed.append({**by_id[finding_id], "merged_into": target["id"]})
            gone.add(finding_id)
    kept = [f for f in by_id.values() if f["id"] not in gone]
    return sorted(kept, key=rank_key), dismissed


def apply_appeal(kept, dismissed, appeal_outputs):
    """Apply the appeal pass: an overturned drop comes back, an overturned downgrade gets its
    severity back (or the severity the appeal set). Returns (kept, dismissed)."""
    decisions = {d["finding_id"]: d for output in appeal_outputs for d in output["decisions"]}
    if not decisions:
        return kept, dismissed

    def annotate(finding):
        decision = decisions.get(finding["id"])
        if not decision:
            return finding
        finding = {**finding, "appeal": {"action": decision["action"], "rationale": decision["rationale"]}}
        if decision["action"] == "overturn":
            finding["severity"] = decision.get("severity") or finding.get("original_severity") or finding["severity"]
        return finding

    restored, still_dismissed = [], []
    for finding in dismissed:
        decision = decisions.get(finding["id"])
        if decision and decision["action"] == "overturn" and not finding.get("merged_into"):
            restored.append(annotate(finding))
        else:
            still_dismissed.append(annotate(finding))
    kept = [annotate(finding) for finding in kept] + restored
    return sorted(kept, key=rank_key), still_dismissed


def appeal_candidates(kept, dismissed, severities):
    """Findings the editors dropped or downgraded from one of `severities`."""
    downgraded = [
        f for f in kept
        if f.get("review", {}).get("action") == "rerate" and f.get("original_severity") in severities
        and SEVERITIES.index(f["severity"]) > SEVERITIES.index(f["original_severity"])
    ]
    dropped = [f for f in dismissed
               if f.get("review", {}).get("action") == "drop" and not f.get("merged_into") and f["severity"] in severities]
    return downgraded + dropped


def apply_chief(kept, chief_output):
    """Apply the chief's drops and mark its priorities. Returns (kept, dismissed, priorities)."""
    if not chief_output:
        return kept, [], []
    drops = {d["finding_id"]: d["rationale"] for d in chief_output["drops"]}
    dismissed = [{**f, "review": {"action": "drop", "rationale": drops[f["id"]]}} for f in kept if f["id"] in drops]
    kept = [f for f in kept if f["id"] not in drops]
    present = {f["id"] for f in kept}
    priorities = [p for p in chief_output["priorities"] if p["finding_id"] in present]
    return kept, dismissed, priorities


def _merge_target(by_id, decisions, finding_id, depth=0):
    """Follow merge chains (a -> b -> c) to the finding that survives."""
    decision = decisions.get(finding_id)
    if decision and decision["action"] == "drop":
        return None
    if decision and decision["action"] == "merge" and depth < 10:
        return _merge_target(by_id, decisions, decision["merge_into"], depth + 1)
    return by_id.get(finding_id)


def _absorb(target, other):
    seen = {(l["path"], l.get("start_line"), l.get("end_line")) for l in target["locations"]}
    target["locations"] = target["locations"] + [
        l for l in other["locations"] if (l["path"], l.get("start_line"), l.get("end_line")) not in seen
    ]
    target["sources"] = sorted(set(target["sources"]) | set(other["sources"]))


def rank_key(finding):
    return (-weight(finding), finding["locations"][0]["path"], finding["locations"][0].get("start_line") or 0)


def weight(finding):
    return SEVERITY_WEIGHT[finding["severity"]] * CONFIDENCE_FACTOR[finding["confidence"]]


def score(findings, manifest):
    """Aggregate weights per file, module, and category and grade the codebase."""
    total_loc = sum(entry["loc"] for entry in manifest.values()) or 1
    per_file = defaultdict(float)
    per_module = defaultdict(float)
    per_category = defaultdict(lambda: {"count": 0, "weight": 0.0})
    per_severity = {severity: 0 for severity in SEVERITIES}
    total = 0.0
    for finding in findings:
        w = weight(finding)
        total += w
        primary = finding["locations"][0]["path"]
        if primary in manifest:
            per_file[primary] += w
            per_module[manifest[primary]["module"]] += w
        else:  # a module-level finding
            per_module[primary] += w
        per_category[finding["category"]]["count"] += 1
        per_category[finding["category"]]["weight"] += w
        per_severity[finding["severity"]] += 1

    module_loc, module_commits = defaultdict(int), defaultdict(int)
    for entry in manifest.values():
        module_loc[entry["module"]] += entry["loc"]
        module_commits[entry["module"]] += entry["commits"]

    index = total / total_loc * 1000
    duplication = duplication_pct(manifest)
    grade, reasons = _grade(index, per_severity, duplication)
    return {
        "slop_index": round(index, 2),
        "grade": grade,
        "grade_reasons": reasons,
        "duplication_pct": duplication,
        "total_loc": total_loc,
        "files_scanned": len(manifest),
        "by_severity": per_severity,
        "by_category": dict(sorted(per_category.items(), key=lambda item: -item[1]["weight"])),
        "hotspot_files": _hotspots(per_file, {p: e["loc"] for p, e in manifest.items()},
                                   {p: e["commits"] for p, e in manifest.items()},
                                   {p: e["fan_in"] for p, e in manifest.items()}),
        "hotspot_modules": _hotspots(per_module, module_loc, module_commits),
    }


def duplication_pct(manifest):
    """Share of non-test lines of code that sit in a copy found by clone detection."""
    code = [e for e in manifest.values() if not e.get("is_test")]
    loc = sum(e["loc"] for e in code) or 1
    return round(100 * sum(e.get("duplicated_lines", 0) for e in code) / loc, 1)


def _grade(index, per_severity, duplication):
    """The worst grade any gate allows, and the gates that set it."""
    caps = [(next((g for limit, g in GRADE_BANDS if index < limit), "F"), f"slop index {index:.2f} per 1k LOC")]
    for severity in ("critical", "high"):
        gate = next(((n, cap) for s, n, cap in SEVERITY_GATES if s == severity and per_severity[severity] >= n), None)
        if gate:
            caps.append((gate[1], f"{per_severity[severity]} {severity} findings (at least {gate[0]} caps the grade at {gate[1]})"))
    gate = next(((pct, cap) for pct, cap in DUPLICATION_GATES if duplication >= pct), None)
    if gate:
        caps.append((gate[1], f"{duplication}% of non-test code is copied (at least {gate[0]}% caps the grade at {gate[1]})"))
    grade = max((cap for cap, _ in caps), key=GRADES.index)
    return grade, [reason for cap, reason in caps if cap == grade]


def _hotspots(weights, loc, commits, fan_in=None, limit=25):
    rows = [
        {"name": name, "weight": round(w, 2), "loc": loc.get(name, 0), "commits": commits.get(name, 0),
         **({"fan_in": fan_in.get(name, 0)} if fan_in is not None else {}),
         "density": round(w / max(loc.get(name, 0), 50) * 1000, 2)}
        for name, w in weights.items()
    ]
    return sorted(rows, key=lambda row: -row["weight"])[:limit]


def _dedupe(raw):
    """Merge findings that describe the same problem at the same place, keeping the strongest."""
    groups = defaultdict(list)
    for finding in raw:
        primary = finding["locations"][0]
        groups[(primary["path"], finding["category"], finding["rule"])].append(finding)

    merged = []
    for candidates in groups.values():
        clusters = []
        for finding in candidates:
            cluster = next((c for c in clusters if _overlaps(c[0], finding)), None)
            if cluster is None:
                clusters.append([finding])
            else:
                cluster.append(finding)
        merged.extend(_merge_cluster(cluster) for cluster in clusters)
    return merged


def _overlaps(a, b):
    a_loc, b_loc = a["locations"][0], b["locations"][0]
    a_start, b_start = a_loc.get("start_line"), b_loc.get("start_line")
    if a_start is None or b_start is None:
        return True  # file-level findings of the same rule on the same file are the same problem
    a_end = a_loc.get("end_line") or a_start
    b_end = b_loc.get("end_line") or b_start
    return a_start <= b_end and b_start <= a_end


def _merge_cluster(cluster):
    best = dict(max(cluster, key=weight))
    best["sources"] = sorted({source for finding in cluster for source in finding["sources"]})
    seen, locations = set(), []
    for finding in [best] + cluster:
        for location in finding["locations"]:
            key = (location["path"], location.get("start_line"), location.get("end_line"))
            if key not in seen:
                seen.add(key)
                locations.append(location)
    best["locations"] = locations
    return best


def _stable_id(finding):
    primary = finding["locations"][0]
    key = "|".join(str(part) for part in (finding["category"], finding["rule"], primary["path"], primary.get("start_line"), finding["title"]))
    return "SLOP-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]


def load_outputs(workspace, tiers):
    """Every finished output for the given tiers. The runner only writes validated outputs."""
    return {
        tier: [read_json(path) for path in sorted(workspace.out_dir(tier).glob("*.json"))]
        for tier in tiers
    }

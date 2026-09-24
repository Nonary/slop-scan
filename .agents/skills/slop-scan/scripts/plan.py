"""Build the task files for each tier from the results of the tiers before it."""

import re
from collections import defaultdict
from pathlib import Path

import findings as findingslib
import fingerprint as fingerprintlib
from inventory import load_manifest
from languages import language_for
from lenses import lenses_for
from packing import pack
from schema import HEALTH, LEVELS, MAX_FLOWS
from workspace import read_json, task_header, task_id_for, write_json

METRIC_KEYS = ("loc", "functions", "longest_function", "long_functions", "max_nesting", "branch_points", "fan_in",
               "fan_out", "is_test", "commits", "age_days", "duplicated_lines", "swallowed_catches", "log_calls",
               "hedges", "narrating_comments", "reach", "reach_roots")
EVIDENCE_KEYS = ("loc", "fan_in", "fan_out", "interface", "commits", "age_days", "co_changes", "structure",
                 "duplicated_lines", "clones", "swallowed_catches", "log_calls", "hedges", "reach", "reach_roots")
EDIT_SCOPES = {
    "all": ("critical", "high", "medium", "low"),
    "medium-up": ("critical", "high", "medium"),
}
APPEAL_SCOPE = ("critical", "high", "medium")  # drops and downgrades from these get a second opinion
MAX_UNIT_CLONES = 12
MAX_FLOW_SYMBOLS = 6
MAX_REFERENCES_PER_SYMBOL = 40
MAX_FLOW_FILES = 30


def plan_tier2(workspace, tier1_outputs, lens_names, module_loc, module_count, module_files):
    """One task per lens per group of modules. A module too big for one focused review is
    split into parts; each part reviews its own files in detail with the rest of the module
    visible as a one-line-per-file overview."""
    manifest = load_manifest(workspace)
    graph = read_json(workspace.graph_file)
    cards = merge_file_cards(card for output in tier1_outputs for card in output.get("files", []))
    local_findings = defaultdict(list)
    for output in tier1_outputs:
        for finding in output["findings"]:
            local_findings[finding["locations"][0]["path"]].append(_brief(finding))

    clone_pairs = workspace.clones()["pairs"]
    files_by_module = defaultdict(list)
    for path, entry in manifest.items():
        files_by_module[entry["module"]].append(path)
    cyclic_modules = {m for cycle in graph["module_cycles"] for m in cycle}
    edge_counts = {(e["from"], e["to"]): e["count"] for e in graph["module_edges"]}

    units = []
    for module in sorted(files_by_module):
        paths = sorted(files_by_module[module])
        stats = graph["modules"][module]
        context = {
            "module": module,
            "module_loc": sum(manifest[p]["loc"] for p in paths),
            "instability": stats["instability"],
            "in_module_cycle": module in cyclic_modules,
            "depends_on": {m: edge_counts[(module, m)] for m in stats["depends_on"]},
            "depended_on_by": {m: edge_counts[(m, module)] for m in stats["depended_on_by"]},
        }
        parts = pack(paths, lambda p: manifest[p]["loc"], module_loc, module_files)
        for number, part_paths in enumerate(parts, 1):
            focus = set(part_paths)
            unit = {
                **context,
                "loc": sum(manifest[p]["loc"] for p in part_paths),
                "internal_imports": [
                    [p, t] for p in paths for t in graph["imports"][p]
                    if manifest[t]["module"] == module and (p in focus or t in focus)
                ],
                "files": [
                    {
                        "path": p,
                        "metrics": {k: manifest[p].get(k) for k in METRIC_KEYS},
                        **({"interface": manifest[p]["interface"]} if "interface" in manifest[p] else {}),
                        "co_changes": manifest[p]["co_changes"],
                        "structure": manifest[p]["structure"],
                        "card": cards.get(p),  # None when the scout failed; read the source instead
                        "tier1_findings": local_findings.get(p, []),
                    }
                    for p in part_paths
                ],
                # Code copied between these files, or from them to elsewhere (clone detection).
                "copies": [_copy(pair) for pair in clone_pairs
                           if pair["a"]["path"] in focus or pair["b"]["path"] in focus][:MAX_UNIT_CLONES],
            }
            if len(parts) > 1:
                unit["part"] = {"number": number, "of": len(parts)}
                unit["rest_of_module"] = [
                    {"path": p, "purpose": (cards.get(p) or {}).get("purpose", "")} for p in paths if p not in focus
                ]
            units.append(unit)

    groups = pack(units, lambda unit: unit["loc"], module_loc, module_count)
    lenses = lenses_for("tier2", lens_names)
    root = workspace.run()["root"]
    for number, group in enumerate(groups, 1):
        for lens in lenses:
            task_id = task_id_for("T2", number, lens)
            write_json(workspace.tasks_dir("tier2") / f"{task_id}.json", {
                **task_header("tier2", task_id, root, lens),
                "modules": group,
            })
    return len(groups) * len(lenses)


def plan_tier3(workspace, tier1_outputs, tier2_outputs, lens_names):
    manifest = load_manifest(workspace)
    graph = read_json(workspace.graph_file)
    module_loc = defaultdict(int)
    for entry in manifest.values():
        module_loc[entry["module"]] += entry["loc"]

    summaries = [
        {**summary, "loc": module_loc.get(summary["module"], 0),
         "instability": graph["modules"].get(summary["module"], {}).get("instability")}
        for summary in merge_module_summaries(s for output in tier2_outputs for s in output.get("modules", []))
    ]
    tier1_findings = [f for output in tier1_outputs for f in output["findings"]]
    tier2_findings = [f for output in tier2_outputs for f in output["findings"]]
    clones = workspace.clones()
    context = {
        "totals": {"files": len(manifest), "loc": sum(module_loc.values()), "modules": len(module_loc)},
        "languages": _language_mix(manifest),
        "history": workspace.run()["history"],
        "hot_files": _hot_files(manifest),
        "duplication": _duplication_brief(clones, manifest),
        "fingerprint": _fingerprint_brief(fingerprintlib.summarize(manifest)),
        "reachability": reach_brief(workspace.reach()),
        "modules": summaries,
        "module_edges": graph["module_edges"],
        "module_cycles": graph["module_cycles"],
        "file_cycles": graph["file_cycles"][:25],
        "preliminary_scores": findingslib.score(tier1_findings + tier2_findings, manifest),
        "top_tier2_findings": [_brief(f) for f in sorted(tier2_findings, key=findingslib.rank_key)[:300]],
        "top_tier1_findings": [_brief(f) for f in sorted(tier1_findings, key=findingslib.rank_key)[:150]],
    }
    lenses = lenses_for("tier3", lens_names)
    root = workspace.run()["root"]
    for lens in lenses:
        task_id = task_id_for("T3", 1, lens)
        write_json(workspace.tasks_dir("tier3") / f"{task_id}.json", {
            **task_header("tier3", task_id, root, lens),
            **context,
        })
    return len(lenses)


def plan_flows(workspace, tier1_outputs, system):
    """One task per flow the systems reviewer nominated. Each carries where the entry points are
    defined and every line that mentions them, so the tracer starts from a map, not a search."""
    manifest = load_manifest(workspace)
    root = workspace.root()
    cards = merge_file_cards(card for output in tier1_outputs for card in output.get("files", []))
    texts = {}

    def lines_of(path):
        if path not in texts:
            try:
                texts[path] = (root / path).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                texts[path] = []
        return texts[path]

    count = 0
    for flow in system.get("flows", [])[:MAX_FLOWS]:
        symbols = []
        for point in flow["entry_points"]:
            name = re.split(r"::|\.", point["symbol"].strip().rstrip("()"))[-1]
            if name and name not in symbols:
                symbols.append(name)
        symbols = symbols[:MAX_FLOW_SYMBOLS]
        if not symbols:
            continue
        pattern = re.compile(r"\b(" + "|".join(re.escape(s) for s in symbols) + r")\b")
        references, per_symbol = [], defaultdict(int)
        for path in sorted(manifest):
            for line_number, line in enumerate(lines_of(path), 1):
                for symbol in set(pattern.findall(line)):
                    if per_symbol[symbol] < MAX_REFERENCES_PER_SYMBOL:
                        per_symbol[symbol] += 1
                        references.append({"path": path, "line": line_number, "symbol": symbol, "text": line.strip()[:160]})
        definitions = []
        for point in flow["entry_points"]:
            name = re.split(r"::|\.", point["symbol"].strip().rstrip("()"))[-1]
            mentions = [r for r in references if r["path"] == point["path"] and r["symbol"] == name]
            language = language_for(Path(point["path"]))
            starts = language.function_start if language else None
            hit = next((r for r in mentions if starts and starts.search(r["text"])), mentions[0] if mentions else None)
            if hit and hit not in definitions:
                definitions.append(hit)
        involved = sorted({r["path"] for r in references} | {p["path"] for p in flow["entry_points"]})[:MAX_FLOW_FILES]
        count += 1
        write_json(workspace.tasks_dir("flows") / f"FL-{count:03d}.json", {
            **task_header("flows", f"FL-{count:03d}", root),
            "flow": flow,
            "definitions": [{k: d[k] for k in ("path", "line", "symbol")} for d in definitions[:4]],
            "references": references,
            "files": [
                {"path": p, "purpose": (cards.get(p) or {}).get("purpose", ""),
                 **{k: manifest[p].get(k) for k in ("loc", "fan_in", "commits", "age_days", "reach", "reach_roots")}}
                for p in involved
            ],
        })
    return count


def plan_appeal(workspace, kept, dismissed, batch_size):
    """Second opinions on what the editors dropped or downgraded from medium or above."""
    manifest = load_manifest(workspace)
    root = workspace.run()["root"]
    candidates = sorted(
        findingslib.appeal_candidates(kept, dismissed, APPEAL_SCOPE),
        key=lambda f: (f["locations"][0]["path"], f["locations"][0].get("start_line") or 0),
    )
    batches = pack(candidates, lambda f: 1, batch_size, batch_size)
    for number, batch in enumerate(batches, 1):
        task_id = f"TA-{number:03d}"
        paths = sorted({loc["path"] for f in batch for loc in f["locations"] if loc["path"] in manifest})
        write_json(workspace.tasks_dir("appeal") / f"{task_id}.json", {
            **task_header("appeal", task_id, root),
            "findings": [_appeal_view(f) for f in batch],
            "evidence": {p: {key: manifest[p].get(key) for key in EVIDENCE_KEYS} for p in paths},
        })
    return len(batches)


def plan_editor(workspace, candidates, scope, batch_size):
    """Batch findings by location, so each editor sees related findings side by side and can
    merge duplicates, with the structural and change evidence for every file involved."""
    manifest = load_manifest(workspace)
    chosen = sorted(
        (f for f in candidates if f["severity"] in EDIT_SCOPES[scope]),
        key=lambda f: (f["locations"][0]["path"], f["locations"][0].get("start_line") or 0),
    )
    batches = pack(chosen, lambda f: 1, batch_size, batch_size)
    root = workspace.run()["root"]
    for number, batch in enumerate(batches, 1):
        task_id = f"TE-{number:03d}"
        paths = sorted({loc["path"] for f in batch for loc in f["locations"] if loc["path"] in manifest})
        write_json(workspace.tasks_dir("editor") / f"{task_id}.json", {
            **task_header("editor", task_id, root),
            "findings": batch,
            "evidence": {p: {key: manifest[p].get(key) for key in EVIDENCE_KEYS} for p in paths},
        })
    return len(batches)


def plan_chief(workspace, kept, module_summaries, system, flows=()):
    """One task: every surviving finding in compact form, plus the big picture."""
    manifest = load_manifest(workspace)
    root = workspace.run()["root"]
    write_json(workspace.tasks_dir("chief") / "TC-001.json", {
        **task_header("chief", "TC-001", root),
        "architecture_summary": system.get("architecture_summary", ""),
        "draft_themes": system.get("themes", []),
        "flows": [{k: f[k] for k in ("name", "summary", "ownership", "health")} for f in flows],
        "history": workspace.run()["history"],
        "duplication": _duplication_brief(workspace.clones(), manifest, limit=8),
        "fingerprint": _fingerprint_brief(fingerprintlib.summarize(manifest), limit=8),
        "reachability": reach_brief(workspace.reach(), limit=8),
        "modules": [
            {k: m[k] for k in ("module", "summary", "cohesion", "coupling", "health")} for m in module_summaries
        ],
        "scores": findingslib.score(kept, manifest),
        "findings": [
            {**_brief(f), "id": f["id"], "effort": f["effort"], "confidence": f["confidence"],
             "impact": f["impact"], **_traffic(manifest.get(f["locations"][0]["path"]))}
            for f in kept
        ],
    })
    return 1


def merge_file_cards(cards):
    """Segmented files get one card per segment; fold them into one card per file."""
    merged = {}
    for card in cards:
        existing = merged.get(card["path"])
        if existing is None:
            merged[card["path"]] = dict(card)
            continue
        existing["purpose"] = f"{existing['purpose']} / {card['purpose']}"
        existing["responsibilities"] = _union(existing["responsibilities"], card["responsibilities"])
        existing["key_symbols"] = _union(existing["key_symbols"], card["key_symbols"])
        existing["health"] = max(existing["health"], card["health"], key=HEALTH.index)
        existing["notes"] = " ".join(filter(None, (existing.get("notes"), card.get("notes"))))
    return merged


def merge_module_summaries(summaries):
    """Split modules get one summary per part; fold them into one per module, keeping the worst ratings."""
    merged = {}
    for summary in summaries:
        existing = merged.get(summary["module"])
        if existing is None:
            merged[summary["module"]] = dict(summary)
            continue
        existing["summary"] = f"{existing['summary']} {summary['summary']}"
        existing["responsibilities"] = _union(existing["responsibilities"], summary["responsibilities"])
        existing["health"] = max(existing["health"], summary["health"], key=HEALTH.index)
        existing["cohesion"] = max(existing["cohesion"], summary["cohesion"], key=LEVELS.index)  # lower is worse
        existing["coupling"] = min(existing["coupling"], summary["coupling"], key=LEVELS.index)  # higher is worse
        existing["notes"] = " ".join(filter(None, (existing.get("notes"), summary.get("notes"))))
    return sorted(merged.values(), key=lambda s: s["module"])


def _traffic(entry):
    if not entry:
        return {}
    return {"commits": entry["commits"], "age_days": entry.get("age_days"), "fan_in": entry["fan_in"],
            "duplicated_lines": entry.get("duplicated_lines", 0)}


def _appeal_view(finding):
    """The finding as the scout or architect reported it, plus what the editor decided."""
    review = finding["review"]
    view = {k: finding[k] for k in ("id", "category", "rule", "title", "locations", "evidence", "impact",
                                    "recommendation", "effort", "confidence")}
    view["severity"] = finding.get("original_severity") or finding["severity"]
    view["first_review"] = {"action": review["action"], "rationale": review["rationale"],
                            **({"severity": finding["severity"]} if review["action"] == "rerate" else {})}
    return view


def _copy(pair):
    a, b = pair["a"], pair["b"]
    return {"a": f"{a['path']}:{a['start_line']}-{a['end_line']}", "b": f"{b['path']}:{b['start_line']}-{b['end_line']}",
            "tokens": pair["tokens"]}


def _hot_files(manifest, limit=25):
    rows = sorted(manifest.items(), key=lambda item: -item[1]["commits"])[:limit]
    return [{"path": p, "commits": e["commits"], "fan_in": e["fan_in"], "loc": e["loc"]} for p, e in rows if e["commits"]]


def _duplication_brief(clones, manifest, limit=20):
    return {
        "duplicated_pct_of_non_test_code": findingslib.duplication_pct(manifest),
        "clone_pairs": clones.get("total_pairs", 0),
        "parallel_trees": clones.get("parallel_trees", [])[:limit // 2],
        "module_pairs": clones.get("module_pairs", [])[:limit],
        "largest_copies": [_copy(pair) for pair in clones.get("pairs", [])[:limit]],
    }


def reach_brief(reach, limit=15):
    """What checked-in configuration runs, trimmed for a prompt or a report; None for older runs."""
    if not reach:
        return None
    return {
        "analyzed": reach["analyzed"],
        "totals": reach["totals"],
        "launchers": reach["launchers"][:limit * 2],
        "conventions": reach["conventions"],
        "self_started": reach["self_started"],
        "library_modules": reach["library_modules"],
        **{key: [{**group, "files": group["files"][:limit], "file_count": len(group["files"])}
                 for group in reach[key][:limit]]
           for key in ("manual", "tests_only", "unreached")},
    }


def _fingerprint_brief(summary, limit=15):
    return {
        "repo_rates": summary["repo"],
        "flagged_modules": [m for m in summary["modules"] if m["flagged"]][:limit],
        "signals": summary["signals"],
        "giant_files": summary["files"]["giant"][:limit],
    }


def _union(first, second):
    return first + [item for item in second if item not in first]


def _brief(finding):
    primary = finding["locations"][0]
    return {
        "severity": finding["severity"],
        "category": finding["category"],
        "rule": finding["rule"],
        "title": finding["title"],
        "path": primary["path"],
        "lines": [primary.get("start_line"), primary.get("end_line")],
    }


def _language_mix(manifest):
    loc = defaultdict(int)
    for entry in manifest.values():
        loc[entry["language"]] += entry["loc"]
    return dict(sorted(loc.items(), key=lambda item: -item[1]))

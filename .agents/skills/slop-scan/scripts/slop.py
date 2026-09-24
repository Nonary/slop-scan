#!/usr/bin/env python3
"""slop-scan: a pragmatic, multi-tier maintainability review run as a pool of `codex exec` workers.

    scan <root>             run the whole pipeline (resumes an existing run)
    status                  progress per tier and token usage
    inventory <root>        tier 0 only: measure files, map imports and history, plan scouts
    plan <tier>             build one tier's tasks from the finished tiers before it
    run <tier>              run one tier's pending tasks
    report                  merge, apply editor, appeal and chief decisions, score, write the reports
    recall --expect FILE    check a finished scan against problems you already know about

Tiers: tier1 scouts -> tier2 architects -> tier3 systems -> flows -> editor -> appeal -> chief.
All state lives in one run directory, so any command can be re-run after an interruption.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import findings as findingslib  # noqa: E402
import fingerprint as fingerprintlib  # noqa: E402
import plan  # noqa: E402
import progress as progresslib  # noqa: E402
import recall as recalllib  # noqa: E402
from inventory import InventorySettings, load_manifest, run_inventory  # noqa: E402
from lenses import default_names  # noqa: E402
from report import build_report_data, write_reports  # noqa: E402
from runner import WorkerSettings, run_tier  # noqa: E402
from workspace import TIERS, Workspace, read_json, write_json  # noqa: E402

RUNS_DIR = Path(".slop_scan")
PREVIOUS = {"tier2": "tier1", "tier3": "tier2", "flows": "tier3", "editor": "flows", "appeal": "editor", "chief": "appeal"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    scan = commands.add_parser("scan", help="run the whole pipeline")
    scan.add_argument("root", nargs="?", help="directory to scan (omit to resume --workdir)")
    _inventory_options(scan)
    _worker_options(scan)
    _plan_options(scan)
    scan.add_argument("--out", help="report directory (default: <workdir>/reports)")

    inventory = commands.add_parser("inventory", help="tier 0 only")
    inventory.add_argument("root")
    _inventory_options(inventory)

    plan_cmd = commands.add_parser("plan", help="build one tier's tasks")
    plan_cmd.add_argument("tier", choices=TIERS[1:])
    plan_cmd.add_argument("--workdir")
    _plan_options(plan_cmd)

    run = commands.add_parser("run", help="run one tier's pending tasks")
    run.add_argument("tier", choices=TIERS)
    run.add_argument("--workdir")
    _worker_options(run)

    status = commands.add_parser("status", help="show progress")
    status.add_argument("--workdir")

    report = commands.add_parser("report", help="write the reports")
    report.add_argument("--workdir")
    report.add_argument("--out")

    recall = commands.add_parser("recall", help="check a finished scan against known problems")
    recall.add_argument("--workdir")
    recall.add_argument("--expect", required=True, metavar="FILE",
                        help='JSON list of {"path", "lines": [start, end], "category", "keywords", "note"}; only path is required')

    args = parser.parse_args(argv)
    {"scan": cmd_scan, "inventory": cmd_inventory, "plan": cmd_plan, "run": cmd_run,
     "status": cmd_status, "report": cmd_report, "recall": cmd_recall}[args.command](args)


def _inventory_options(parser):
    parser.add_argument("--workdir", help="run directory (default: ./.slop_scan/<root name>)")
    parser.add_argument("--shard-loc", type=int, default=1200, help="max lines of code per scout task")
    parser.add_argument("--shard-files", type=int, default=8, help="max files or segments per scout task")
    parser.add_argument("--chunk-lines", type=int, default=900, help="split files longer than this into segments")
    parser.add_argument("--deep", action="store_true",
                        help="review every shard, module group and the system through 3 narrow lenses (about 3x the tasks)")
    parser.add_argument("--no-tests", dest="include_tests", action="store_false", help="skip test files")
    parser.add_argument("--exclude", action="append", default=[], metavar="GLOB", help="skip matching paths (repeatable)")
    parser.add_argument("--ext", action="append", default=[], metavar=".EXT=LANG", help="treat an extension as a language")
    parser.add_argument("--history-days", type=int, default=365, help="churn window in days, ending at the newest commit")


def _worker_options(parser):
    parser.add_argument("--parallel", type=int, default=6, help="concurrent codex exec workers")
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--effort", default="medium", help="model_reasoning_effort for workers")
    parser.add_argument("--timeout", type=int, default=1200, help="seconds before a worker is killed")


def _plan_options(parser):
    parser.add_argument("--module-loc", type=int, default=6000, help="tier2: max lines of code per architect task")
    parser.add_argument("--module-count", type=int, default=4, help="tier2: max modules per architect task")
    parser.add_argument("--module-files", type=int, default=16, help="tier2: max files per architect task")
    parser.add_argument("--edit-scope", choices=tuple(plan.EDIT_SCOPES), default="all", help="editor: which findings to review")
    parser.add_argument("--edit-batch", type=int, default=10, help="editor: findings per editor task")
    parser.add_argument("--appeal-batch", type=int, default=10, help="appeal: findings per appeal task")


# -- commands -------------------------------------------------------------------------------

def cmd_scan(args):
    workspace = _workspace(args, root=args.root)
    if not workspace.run_file.exists():
        if not args.root:
            sys.exit("Pass the directory to scan.")
        _inventory(workspace, args)
    settings = _worker_settings(args)
    for tier in TIERS:
        if progresslib.progress(workspace, tier) is None:
            _plan(workspace, tier, args)
        state = run_tier(workspace, tier, args.parallel, settings)
        if state["failed"]:
            print(f"{tier}: {len(state['failed'])} task(s) failed after {progresslib.MAX_ATTEMPTS} attempts; "
                  f"continuing without them: {state['failed']}")
    _report(workspace, args.out)


def cmd_inventory(args):
    _inventory(_workspace(args, root=args.root), args)


def cmd_plan(args):
    workspace = _workspace(args)
    if progresslib.progress(workspace, args.tier) is not None:
        sys.exit(f"{args.tier} is already planned.")
    _plan(workspace, args.tier, args)


def cmd_run(args):
    workspace = _workspace(args)
    if progresslib.progress(workspace, args.tier) is None:
        sys.exit(f"{args.tier} is not planned yet: run `plan {args.tier}` first.")
    run_tier(workspace, args.tier, args.parallel, _worker_settings(args))


def cmd_status(args):
    workspace = _workspace(args)
    for tier in TIERS:
        state = progresslib.progress(workspace, tier)
        if state is None:
            print(f"{tier:7} not planned")
        else:
            print(f"{tier:7} {len(state['done'])}/{state['tasks']} done, {len(state['pending'])} pending, {len(state['failed'])} failed")
    usage_file = workspace.dir / "usage.json"
    if usage_file.exists():
        print("usage  ", json.dumps(read_json(usage_file)))
    reports = workspace.reports_dir / "SLOP_REPORT.md"
    if reports.exists():
        print(f"report  {reports}")


def cmd_report(args):
    _report(_workspace(args), args.out)


def cmd_recall(args):
    workspace = _workspace(args)
    if not workspace.findings_file.exists() or "dismissed" not in read_json(workspace.findings_file):
        sys.exit("No finished report in this run yet: run `report` first.")
    result = recalllib.check(read_json(workspace.findings_file), read_json(args.expect))
    write_json(workspace.dir / "recall.json", result)
    print(recalllib.render(result))


# -- steps ----------------------------------------------------------------------------------

def _inventory(workspace, args):
    if workspace.run_file.exists():
        sys.exit(f"{workspace.dir} already holds a run. Resume it with `scan --workdir {workspace.dir}`.")
    settings = InventorySettings(
        shard_loc=args.shard_loc,
        shard_files=args.shard_files,
        chunk_lines=args.chunk_lines,
        lenses=default_names("tier1") if args.deep else ["all"],
        include_tests=args.include_tests,
        exclude=args.exclude,
        extra_extensions=dict(item.split("=", 1) for item in args.ext),
        history_days=args.history_days,
    )
    summary = run_inventory(workspace, Path(args.root).resolve(), settings)
    print(json.dumps({"workdir": str(workspace.dir), **summary}, indent=2))


def _plan(workspace, tier, args):
    previous = progresslib.progress(workspace, PREVIOUS[tier])
    if previous is None or previous["pending"]:
        sys.exit(f"Cannot plan {tier}: {PREVIOUS[tier]} is not finished. Run it first.")
    workspace.tasks_dir(tier).mkdir(parents=True)
    outputs = findingslib.load_outputs(workspace, TIERS)
    deep = workspace.run()["settings"]["lenses"] != ["all"]

    if tier == "tier2":
        count = plan.plan_tier2(workspace, outputs["tier1"], _lenses("tier2", deep),
                                args.module_loc, args.module_count, args.module_files)
    elif tier == "tier3":
        count = plan.plan_tier3(workspace, outputs["tier1"], outputs["tier2"], _lenses("tier3", deep))
    elif tier == "flows":
        count = plan.plan_flows(workspace, outputs["tier1"], _system(outputs))
    elif tier == "editor":
        candidates = findingslib.collect(workspace, outputs)
        write_json(workspace.findings_file, {"findings": candidates})
        count = plan.plan_editor(workspace, candidates, args.edit_scope, args.edit_batch)
    elif tier == "appeal":
        kept, dismissed = findingslib.apply_editor(findingslib.collect(workspace, outputs), outputs["editor"])
        count = plan.plan_appeal(workspace, kept, dismissed, args.appeal_batch)
    else:
        kept, _ = _reviewed(workspace, outputs)
        count = plan.plan_chief(workspace, kept, _module_summaries(outputs), _system(outputs), _flows(outputs)) if kept else 0
    print(f"{tier}: planned {count} task(s)")


def _report(workspace, out):
    outputs = findingslib.load_outputs(workspace, TIERS)
    manifest = load_manifest(workspace)
    kept, dismissed = _reviewed(workspace, outputs)
    chief = outputs["chief"][0] if outputs["chief"] else {}
    kept, chief_dismissed, priorities = findingslib.apply_chief(kept, chief)
    dismissed += chief_dismissed
    scores = findingslib.score(kept, manifest)
    coverage = {}
    for tier in TIERS:
        state = progresslib.progress(workspace, tier)
        coverage[tier] = {"tasks": state["tasks"], "done": len(state["done"])} if state else {"tasks": 0, "done": 0}
    usage_file = workspace.dir / "usage.json"
    data = build_report_data(
        workspace.run(), manifest, scores, kept, dismissed, priorities, _module_summaries(outputs),
        _system(outputs), chief, coverage, read_json(usage_file) if usage_file.exists() else None,
        # Runs inventoried before the fingerprint counters existed have nothing to show.
        fingerprint=fingerprintlib.summarize(manifest) if any("log_calls" in e for e in manifest.values()) else None,
        duplication=workspace.clones(), flows=_flows(outputs), reach=plan.reach_brief(workspace.reach(), limit=25),
    )
    write_json(workspace.findings_file, {"findings": kept, "dismissed": dismissed})
    markdown, html = write_reports(data, out or workspace.reports_dir)
    print(json.dumps({
        "grade": scores["grade"],
        "grade_reasons": scores["grade_reasons"],
        "slop_index": scores["slop_index"],
        "duplication_pct": scores["duplication_pct"],
        "findings": len(kept),
        "dismissed": len(dismissed),
        "restored_on_appeal": sum(1 for f in kept if f.get("appeal", {}).get("action") == "overturn"),
        "by_severity": scores["by_severity"],
        "markdown": str(markdown),
        "html": str(html),
    }, indent=2))


def _reviewed(workspace, outputs):
    """Merged findings after the editor and appeal passes: (kept, dismissed)."""
    kept, dismissed = findingslib.apply_editor(findingslib.collect(workspace, outputs), outputs["editor"])
    return findingslib.apply_appeal(kept, dismissed, outputs["appeal"])


# -- helpers --------------------------------------------------------------------------------

def _workspace(args, root=None):
    if args.workdir:
        return Workspace(args.workdir)
    if root:
        return Workspace(RUNS_DIR / Path(root).resolve().name)
    runs = sorted(p.parent for p in RUNS_DIR.glob("*/run.json"))
    if len(runs) != 1:
        sys.exit(f"Pass --workdir; found {len(runs)} runs under {RUNS_DIR.resolve()}: {[str(r) for r in runs]}")
    return Workspace(runs[0])


def _worker_settings(args):
    return WorkerSettings(model=args.model, effort=args.effort, timeout=args.timeout)


def _lenses(tier, deep):
    return default_names(tier) if deep else ["all"]


def _module_summaries(outputs):
    return plan.merge_module_summaries(m for output in outputs["tier2"] for m in output.get("modules", []))


def _system(outputs):
    return next((o for o in outputs["tier3"] if o.get("themes")), {})


def _flows(outputs):
    return [o["flow"] for o in outputs["flows"] if o.get("flow")]


if __name__ == "__main__":
    main()

"""Validation of worker outputs against references/contracts.md.

Workers are cheap models, so the runner is forgiving about details and strict about shape:
`sanitize` drops individual findings or decisions that are malformed, and `validate_output`
reports only the structural problems that make an output unusable (these trigger a repair).
"""

from collections import defaultdict

CATEGORIES = {
    "cohesion", "coupling", "complexity", "duplication", "abstraction", "layering",
    "naming", "dead-weight", "error-design", "consistency", "testability", "slop-signals",
}
SEVERITIES = ("critical", "high", "medium", "low")
CONFIDENCES = ("high", "medium", "low")
EFFORTS = ("S", "M", "L")
HEALTH = ("clean", "minor", "concerning", "sloppy")
LEVELS = ("high", "medium", "low")
ACTIONS = ("keep", "rerate", "merge", "drop")
APPEAL_ACTIONS = ("uphold", "overturn")
SEGMENT_SLACK = 10  # lines a segment finding may reach past its boundary
MAX_PRIORITIES = 15
MAX_FLOWS = 6


class Errors(list):
    def need(self, condition, message):
        if not condition:
            self.append(message)
        return condition

    def text(self, obj, key, where):
        return self.need(isinstance(obj.get(key), str) and obj[key].strip(), f"{where}: `{key}` must be a non-empty string")

    def choice(self, obj, key, allowed, where):
        return self.need(obj.get(key) in allowed, f"{where}: `{key}` must be one of {list(allowed)}, got {obj.get(key)!r}")

    def string_list(self, obj, key, where):
        value = obj.get(key)
        return self.need(
            isinstance(value, list) and all(isinstance(v, str) for v in value),
            f"{where}: `{key}` must be a list of strings",
        )


# -- sanitize: keep what is usable, report what was dropped ---------------------------------

def sanitize(tier, output, task, manifest):
    """Return (output, dropped) where malformed findings/decisions have been removed."""
    output = dict(output)
    output["task_id"] = task["task_id"]
    dropped = []
    if tier in ("tier1", "tier2", "tier3", "flows"):
        scope = _tier1_scope(task) if tier == "tier1" else None
        categories = task["lens"]["categories"] if "lens" in task else CATEGORIES
        kept = []
        for number, finding in enumerate(output.get("findings") or []):
            problems = finding_errors(finding, f"findings[{number}]", manifest, scope, categories)
            (dropped.extend(problems) if problems else kept.append(finding))
        output["findings"] = kept
        if tier == "tier3" and task["lens"]["primary"]:
            output["flows"], problems = _usable(output.get("flows"), "flows", lambda f, where: _nomination_errors(f, where, manifest))
            dropped.extend(problems)
            output["flows"] = output["flows"][:MAX_FLOWS]
    elif tier == "appeal":
        known = {f["id"] for f in task["findings"]}
        output["decisions"], problems = _usable(output.get("decisions"), "decisions", lambda d, where: _appeal_errors(d, where, known))
        dropped.extend(problems)
    elif tier == "editor":
        known = {f["id"] for f in task["findings"]}
        output["decisions"], problems = _usable(output.get("decisions"), "decisions", lambda d, where: _decision_errors(d, where, known))
        dropped.extend(problems)
    elif tier == "chief":
        known = {f["id"] for f in task["findings"]}
        output["priorities"], problems = _usable(output.get("priorities"), "priorities", lambda p, where: _reference_errors(p, "why", where, known))
        dropped.extend(problems)
        output["priorities"] = output["priorities"][:MAX_PRIORITIES]
        output["drops"], problems = _usable(output.get("drops"), "drops", lambda d, where: _reference_errors(d, "rationale", where, known))
        dropped.extend(problems)
    return output, dropped


def _usable(items, name, check):
    kept, problems = [], []
    for number, item in enumerate(items or []):
        errors = check(item, f"{name}[{number}]")
        (problems.extend(errors) if errors else kept.append(item))
    return kept, problems


# -- validate: structural problems that make an output unusable ------------------------------

def validate_output(tier, output, task, manifest):
    """Return a list of human-readable problems; empty means valid. Run after `sanitize`."""
    errors = Errors()
    if not errors.need(isinstance(output, dict), "top level must be a JSON object"):
        return errors
    {
        "tier1": _validate_tier1,
        "tier2": _validate_tier2,
        "tier3": _validate_tier3,
        "flows": _validate_flow,
        "editor": _validate_editor,
        "appeal": _validate_editor,
        "chief": _validate_chief,
    }[tier](output, task, errors)
    if tier in ("tier1", "tier2", "tier3", "flows"):
        errors.need(isinstance(output.get("findings"), list), "`findings` must be a list (use [] when there are none)")
    return errors


def _validate_tier1(output, task, errors):
    cards = output.get("files")
    if not task["lens"]["primary"]:
        return
    expected = {f["path"] for f in task["files"]}
    if not errors.need(isinstance(cards, list), "`files` must be a list with one card per task file"):
        return
    seen = set()
    for number, card in enumerate(cards):
        where = f"files[{number}]"
        if not errors.need(isinstance(card, dict), f"{where} must be an object"):
            continue
        if card.get("path") not in expected:
            continue  # a stray card is harmless; ignore it
        seen.add(card["path"])
        errors.text(card, "purpose", where)
        errors.string_list(card, "responsibilities", where)
        errors.string_list(card, "key_symbols", where)
        errors.choice(card, "health", HEALTH, where)
    missing = sorted(expected - seen)
    errors.need(not missing, f"`files` is missing cards for: {missing}")


def _validate_tier2(output, task, errors):
    summaries = output.get("modules")
    if not task["lens"]["primary"]:
        return
    expected = {m["module"] for m in task["modules"]}
    if not errors.need(isinstance(summaries, list), "`modules` must be a list with one entry per task module"):
        return
    seen = set()
    for number, summary in enumerate(summaries):
        where = f"modules[{number}]"
        if not errors.need(isinstance(summary, dict), f"{where} must be an object"):
            continue
        if summary.get("module") not in expected:
            continue
        seen.add(summary["module"])
        errors.text(summary, "summary", where)
        errors.string_list(summary, "responsibilities", where)
        errors.choice(summary, "cohesion", LEVELS, where)
        errors.choice(summary, "coupling", LEVELS, where)
        errors.choice(summary, "health", HEALTH, where)
    missing = sorted(expected - seen)
    errors.need(not missing, f"`modules` is missing entries for: {missing}")


def _validate_tier3(output, task, errors):
    if not task["lens"]["primary"]:
        return
    errors.text(output, "architecture_summary", "top level")
    _validate_themes(output, errors)
    errors.need(isinstance(output.get("flows"), list), "`flows` must be a list of 0-6 flows to trace")


def _validate_flow(output, task, errors):
    flow = output.get("flow")
    if errors.need(isinstance(flow, dict), "`flow` must be an object describing the traced flow"):
        errors.text(flow, "name", "flow")
        errors.text(flow, "summary", "flow")
        errors.string_list(flow, "steps", "flow")
        errors.text(flow, "ownership", "flow")
        errors.choice(flow, "health", HEALTH, "flow")


def _validate_editor(output, task, errors):
    errors.need(isinstance(output.get("decisions"), list), "`decisions` must be a list with one entry per finding")


def _validate_chief(output, task, errors):
    errors.text(output, "executive_summary", "top level")
    _validate_themes(output, errors)
    errors.need(output.get("priorities"), "`priorities` must list 1-15 findings to fix first")


def _validate_themes(output, errors):
    themes = output.get("themes")
    if errors.need(isinstance(themes, list) and 1 <= len(themes) <= 8, "`themes` must be a list of 1-8 objects"):
        for number, theme in enumerate(themes):
            if errors.need(isinstance(theme, dict), f"themes[{number}] must be an object"):
                errors.text(theme, "title", f"themes[{number}]")
                errors.text(theme, "description", f"themes[{number}]")


# -- item checks ---------------------------------------------------------------------------

def finding_errors(finding, where, manifest, scope, categories):
    errors = Errors()
    if not errors.need(isinstance(finding, dict), f"{where} must be an object"):
        return errors
    errors.choice(finding, "category", sorted(categories), where)
    errors.need(
        isinstance(finding.get("rule"), str) and finding["rule"] == finding["rule"].strip().lower() and " " not in finding["rule"],
        f"{where}: `rule` must be a kebab-case slug such as 'god-class'",
    )
    errors.choice(finding, "severity", SEVERITIES, where)
    errors.choice(finding, "confidence", CONFIDENCES, where)
    errors.choice(finding, "effort", EFFORTS, where)
    for key in ("title", "evidence", "impact", "recommendation"):
        errors.text(finding, key, where)
    locations = finding.get("locations")
    if errors.need(isinstance(locations, list) and locations, f"{where}: `locations` must be a non-empty list"):
        for number, location in enumerate(locations):
            _location_errors(location, f"{where}.locations[{number}]", manifest, scope if number == 0 else None, errors)
    return errors


def _decision_errors(decision, where, known):
    errors = Errors()
    if not errors.need(isinstance(decision, dict), f"{where} must be an object"):
        return errors
    errors.need(decision.get("finding_id") in known, f"{where}: unknown finding_id {decision.get('finding_id')!r}")
    errors.choice(decision, "action", ACTIONS, where)
    errors.text(decision, "rationale", where)
    if decision.get("action") == "rerate":
        errors.choice(decision, "severity", SEVERITIES, where)
    if decision.get("action") == "merge":
        errors.need(decision.get("merge_into") in known - {decision.get("finding_id")},
                    f"{where}: `merge_into` must be another finding id from this batch")
    return errors


def _appeal_errors(decision, where, known):
    errors = Errors()
    if not errors.need(isinstance(decision, dict), f"{where} must be an object"):
        return errors
    errors.need(decision.get("finding_id") in known, f"{where}: unknown finding_id {decision.get('finding_id')!r}")
    errors.choice(decision, "action", APPEAL_ACTIONS, where)
    errors.text(decision, "rationale", where)
    if decision.get("action") == "overturn":
        errors.choice(decision, "severity", SEVERITIES, where)
    return errors


def _nomination_errors(flow, where, manifest):
    errors = Errors()
    if not errors.need(isinstance(flow, dict), f"{where} must be an object"):
        return errors
    errors.text(flow, "name", where)
    errors.text(flow, "why", where)
    points = flow.get("entry_points")
    if errors.need(isinstance(points, list) and points, f"{where}: `entry_points` must be a non-empty list"):
        for number, point in enumerate(points):
            errors.need(
                isinstance(point, dict) and point.get("path") in manifest
                and isinstance(point.get("symbol"), str) and point["symbol"].strip(),
                f"{where}.entry_points[{number}] must be {{path: a scanned file, symbol: a function or method name}}",
            )
    return errors


def _reference_errors(item, text_key, where, known):
    errors = Errors()
    if errors.need(isinstance(item, dict), f"{where} must be an object"):
        errors.need(item.get("finding_id") in known, f"{where}: unknown finding_id {item.get('finding_id')!r}")
        errors.text(item, text_key, where)
    return errors


def _location_errors(location, where, manifest, scope, errors):
    if not errors.need(isinstance(location, dict), f"{where} must be an object"):
        return
    path = location.get("path")
    known = manifest.get(path)
    if not errors.need(known is not None or _is_module(path, manifest), f"{where}: path {path!r} is not a scanned file or module"):
        return
    start, end = location.get("start_line"), location.get("end_line")
    for key, value in (("start_line", start), ("end_line", end)):
        if value is not None:
            errors.need(isinstance(value, int) and value >= 1, f"{where}: `{key}` must be a positive integer or null")
    if known and isinstance(start, int) and isinstance(end, int):
        errors.need(start <= end <= known["lines"] + 1, f"{where}: line range {start}-{end} is outside the file ({known['lines']} lines)")
    if scope is not None:
        _scope_errors(path, start, where, scope, errors)


def _tier1_scope(task):
    """Map each task path to the line ranges this scout may cite (None = the whole file)."""
    scope = defaultdict(list)
    for entry in task["files"]:
        segment = entry.get("segment")
        scope[entry["path"]].append((segment["start_line"], segment["end_line"]) if segment else None)
    return scope


def _scope_errors(path, start, where, scope, errors, slack=SEGMENT_SLACK):
    """A scout's primary location must be a file (and segment) it was assigned."""
    if not errors.need(path in scope, f"{where}: the first location must be a file in this task, not {path!r}"):
        return
    ranges = [r for r in scope[path] if r is not None]
    if len(ranges) < len(scope[path]) or not isinstance(start, int):
        return  # whole file assigned, or no line to check
    errors.need(
        any(low - slack <= start <= high + slack for low, high in ranges),
        f"{where}: line {start} is outside the assigned segment(s) {ranges} of {path}",
    )


def _is_module(path, manifest):
    return isinstance(path, str) and any(entry["module"] == path for entry in manifest.values())

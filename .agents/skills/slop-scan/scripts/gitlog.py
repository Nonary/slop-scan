"""Change history from git: how often each file changes (churn), how new it is, and which
files change with it.

Churn is counted over a recent window (a year by default, ending at the newest commit),
because that is where the team works now. The repository's full size and age are measured
separately, so an old, stable codebase is never mistaken for a new one. Each file is judged
on its own numbers: many commits means a hot file; few commits means stable code (it predates
the window) or new code (`age_days` is small). Co-change pairs are withheld only when the
whole repository is young, because early scaffolding commits make everything look coupled.
"""

import datetime
import statistics
import subprocess
from collections import Counter, defaultdict
from itertools import combinations

WINDOW_DAYS = 365
MAX_COMMITS = 20000  # safety cap for very large repositories; the summary says when it was hit
BULK_COMMIT_FILES = 40  # scaffolding drops, formatting sweeps and renames say nothing about coupling
TOP_PARTNERS = 5
MIN_SHARED_COMMITS = 3  # fewer is coincidence
YOUNG = {"total_commits": 50, "age_days": 30}  # below either, the whole repository is new
HOT_COMMITS = 20  # a file with this many commits in the window is hot


def change_history(root, paths, window_days=WINDOW_DAYS):
    """Return (summary, per_file, commits).

    per_file[path] = {"commits": n in the window, "age_days": days since the file was added
    (None when it predates the window), "co_changes": [[partner, shared, ratio], ...]}.
    commits = [(unix time, {scanned paths touched})] inside the window, newest first.
    """
    known = set(paths)
    facts = _repository_facts(root)
    if facts is None:
        empty = {path: {"commits": 0, "age_days": None, "co_changes": []} for path in paths}
        return {"maturity": "none", "total_commits": 0, "window_days": window_days, "window_commits": 0}, empty, []

    since = facts["head"] - window_days * 86400
    commits, added = _window(root, known, since)
    churn = Counter()
    shared = defaultdict(Counter)
    for _, files in commits:
        churn.update(files)
        if len(files) <= BULK_COMMIT_FILES:
            for a, b in combinations(sorted(files), 2):
                shared[a][b] += 1
                shared[b][a] += 1

    young = facts["total_commits"] < YOUNG["total_commits"] or facts["age_days"] < YOUNG["age_days"]
    summary = {
        "maturity": "young" if young else "established",
        "total_commits": facts["total_commits"],
        "first_commit": _date(facts["first"]),
        "last_commit": _date(facts["head"]),
        "age_days": facts["age_days"],
        "window_days": window_days,
        "window_commits": len(commits),
        "capped": len(commits) >= MAX_COMMITS,
        "bulk_commits": sum(1 for _, files in commits if len(files) > BULK_COMMIT_FILES),
        "median_file_commits": statistics.median([churn[p] for p in paths]) if paths else 0,
        "hot_files": sum(1 for p in paths if churn[p] >= HOT_COMMITS),
        "new_files": sum(1 for p in paths if p in added),
    }

    per_file = {}
    for path in paths:
        partners = []
        if not young:
            partners = [
                # ratio: how often the pair moves together, relative to the quieter of the two files
                [partner, count, round(count / min(churn[path], churn[partner]), 2)]
                for partner, count in shared[path].items()
                if count >= MIN_SHARED_COMMITS
            ]
            partners.sort(key=lambda p: (-p[1], -p[2]))
        age = round((facts["head"] - added[path]) / 86400) if path in added else None
        per_file[path] = {"commits": churn[path], "age_days": age, "co_changes": partners[:TOP_PARTNERS]}
    return summary, per_file, commits


def tree_cochange(commits, tree_a, tree_b):
    """How often two directory trees change in the same commit. None when there is too little data."""
    prefix_a, prefix_b = tree_a.rstrip("/") + "/", tree_b.rstrip("/") + "/"
    touches_a = touches_b = both = 0
    for _, files in commits:
        if len(files) > BULK_COMMIT_FILES:
            continue
        in_a = any(f.startswith(prefix_a) for f in files)
        in_b = any(f.startswith(prefix_b) for f in files)
        touches_a += in_a
        touches_b += in_b
        both += in_a and in_b
    quieter = min(touches_a, touches_b)
    if quieter < MIN_SHARED_COMMITS:
        return None
    return {"commits_a": touches_a, "commits_b": touches_b, "both": both, "ratio": round(both / quieter, 2)}


def _repository_facts(root):
    """Total commits, first and newest commit times for the scanned directory, or None without git."""
    times = _git(root, "log", "--no-merges", "--format=%ct", "--", ".")
    if not times:
        return None
    stamps = [int(line) for line in times.split() if line.isdigit()]
    if not stamps:
        return None
    first, head = min(stamps), max(stamps)
    return {"total_commits": len(stamps), "first": first, "head": head, "age_days": round((head - first) / 86400)}


def _window(root, known, since):
    """Commits in the window touching scanned files, newest first, plus when each file was added."""
    log = _git(
        root, "-c", "core.quotePath=false", "log", "--no-merges", "--relative", "-M", "--name-status",
        "--format=format:@@%ct", f"--since={_date(since, full=True)}", f"-n{MAX_COMMITS}", "--", ".",
    )
    commits, added = [], {}
    for block in (log or "").split("@@")[1:]:
        header, _, body = block.partition("\n")
        if not header.strip().isdigit():
            continue
        stamp = int(header.strip())
        if stamp < since:
            continue
        files = set()
        for line in body.splitlines():
            parts = line.split("\t")
            if len(parts) < 2 or parts[-1] not in known:
                continue
            path = parts[-1]
            files.add(path)
            if parts[0].startswith("A") and path not in added:
                added[path] = stamp  # newest first: the most recent time the file was created
        if files:
            commits.append((stamp, files))
    return commits, added


def _git(root, *args):
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True,
                              text=True, errors="replace").stdout
    except (OSError, subprocess.CalledProcessError):
        return None


def _date(stamp, full=False):
    moment = datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc)
    return moment.strftime("%Y-%m-%d %H:%M:%S +0000") if full else moment.strftime("%Y-%m-%d")

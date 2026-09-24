"""Deterministic duplication evidence: copied code (token clones) and parallel directory trees.

Scouts review neighbouring files and architects read summaries, so a block copied from one
corner of the repository into another is invisible to every worker. This pass finds it.

Clones: comments, whitespace, import/include lines and the values of literals are ignored;
identifiers are kept. That finds copy-paste and lightly edited copies, not independent
reimplementations of the same idea. Winnowing (Schleimer, Wilkerson and Aiken, 2003) keeps a
sparse set of k-gram fingerprints that still guarantees any shared run of K + WINDOW - 1
tokens is seen; each shared fingerprint is then extended token by token to the whole copy.

Parallel trees: sibling directories that hold many files with the same names (`web/` and
`web-legacy/`, `platform/linux/` and `platform/windows/`). Some are by design (one backend per
platform), some are a second copy of the product. How often the two trees change in the same
commit tells them apart, so that is reported too.
"""

import posixpath
import re
from array import array
from collections import defaultdict, deque
from itertools import combinations

from gitlog import tree_cochange

K = 30  # tokens per fingerprinted k-gram
WINDOW = 40  # k-grams per winnowing window: any shared run of K + WINDOW - 1 = 69 tokens is found
MIN_TOKENS = 90  # smaller copies are idioms, not duplication worth a reader's time
MIN_LINES = 8
MIN_DISTINCT = 15  # distinct tokens in a copy; filters data tables and repeated one-liners
MAX_OCCURRENCES = 12  # a fingerprint shared by more places is boilerplate, not a copy
MERGE_GAP = 25  # tokens: copies of one block interrupted by small edits are reported as one
MAX_PAIRS = 3000
TOP_PER_FILE = 5
_MOD = (1 << 61) - 1
_BASE = 1_000_003

_TOKEN = re.compile(
    r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|`(?:\\.|[^`\\\n])*`'  # string literals
    r"|\d[\w.]*"  # numbers
    r"|[A-Za-z_$][\w$]*"  # identifiers and keywords
    r"|::|->|=>|==|!=|<=|>=|&&|\|\||<<|>>|\+\+|--|\S"
)
_SKIP_LINE = re.compile(
    r"^\s*(?:#\s*(?:include|import|pragma)\b|import\s|from\s+\S+\s+import\s|export\s+(?:\*|\{[^}]*\})\s+from\s"
    r"|using\s+(?:namespace\s+)?[\w.:]+\s*;|package\s+[\w.]+|use\s+[\w:{}, ]+;|require(?:_relative)?[\s(])"
)
_BLOCK_COMMENT_INLINE = re.compile(r"/\*.*?\*/")
_GENERIC_STEMS = {"index", "main", "__init__", "types", "utils", "util", "helpers", "constants", "cmakelists",
                  "readme", "conftest", "mod", "lib"}
MIN_TREE_FILES = 5
MIN_SHARED_NAMES = 5
MIN_SHARED_RATIO = 0.2
MIN_SHARED_NAMES_ANY_RATIO = 12  # this many shared names is a parallel tree even in large directories
MIN_TREE_COPIED_TOKENS = 1000


def detect(sources, kinds_for):
    """sources: {path: (language, text)}; kinds_for(text, language) -> per-line 'code'/'comment'/'blank'.

    Returns {"pairs": [...], "per_file": {path: {"duplicated_lines", "clones"}}} where each pair is
    {"a": {path, start_line, end_line}, "b": {...}, "tokens": n}, largest first.
    """
    vocab = {}
    paths = sorted(sources)
    streams = []
    for path in paths:
        language, text = sources[path]
        streams.append(_tokenize(text, language, kinds_for, vocab))

    index = defaultdict(list)
    for number, (ids, _) in enumerate(streams):
        for fingerprint, position in _winnow(ids):
            index[fingerprint].append((number, position))

    regions = defaultdict(list)  # (file a, file b) -> [(start a, end a, start b, end b)], token offsets
    seen = defaultdict(list)
    for places in index.values():
        if not 2 <= len(places) <= MAX_OCCURRENCES:
            continue
        for (fa, pa), (fb, pb) in combinations(sorted(places), 2):
            if fa == fb and pb - pa < K:
                continue
            key = (fa, fb)
            if any(sa <= pa < ea and pb - pa == sb - sa for sa, ea, sb, _ in seen[key]):
                continue  # already inside a copy found from another fingerprint
            region = _extend(streams[fa][0], pa, streams[fb][0], pb, same_file=fa == fb)
            if region:
                seen[key].append(region)
                if _worth_reporting(streams[fa], streams[fb], region):
                    regions[key].append(region)

    pairs, duplicated, by_path = [], defaultdict(set), defaultdict(list)
    for (fa, fb), found in regions.items():
        lines_a, lines_b = streams[fa][1], streams[fb][1]
        for sa, ea, sb, eb, matched in _merge(found):
            pairs.append({
                "a": {"path": paths[fa], "start_line": lines_a[sa], "end_line": lines_a[ea - 1]},
                "b": {"path": paths[fb], "start_line": lines_b[sb], "end_line": lines_b[eb - 1]},
                "tokens": matched,
            })
            duplicated[paths[fa]].update(lines_a[sa:ea])
            duplicated[paths[fb]].update(lines_b[sb:eb])
    pairs.sort(key=lambda p: (-p["tokens"], p["a"]["path"], p["a"]["start_line"]))
    for pair in pairs:
        by_path[pair["a"]["path"]].append(pair)
        if pair["b"]["path"] != pair["a"]["path"]:
            by_path[pair["b"]["path"]].append(pair)

    per_file = {
        path: {
            "duplicated_lines": len(duplicated.get(path, ())),
            "clones": [_as_seen_from(path, p) for p in by_path.get(path, [])[:TOP_PER_FILE]],
        }
        for path in paths
    }
    return {"pairs": pairs[:MAX_PAIRS], "total_pairs": len(pairs), "per_file": per_file}


def module_pairs(pairs, module_of, limit=40):
    """Aggregate clone pairs by the (module, module) they connect, largest first."""
    totals = defaultdict(lambda: {"pairs": 0, "tokens": 0, "files": set()})
    for pair in pairs:
        a, b = pair["a"]["path"], pair["b"]["path"]
        key = tuple(sorted((module_of(a), module_of(b))))
        totals[key]["pairs"] += 1
        totals[key]["tokens"] += pair["tokens"]
        totals[key]["files"].update((a, b))
    rows = [{"modules": list(key), "pairs": row["pairs"], "tokens": row["tokens"], "files": len(row["files"])}
            for key, row in totals.items()]
    return sorted(rows, key=lambda r: -r["tokens"])[:limit]


def parallel_trees(paths, loc, commits, pairs, limit=25):
    """Sibling directories that look like two versions of one thing: many files with the same
    names, or much code copied between them. Each comes with how often the two change together."""
    names = defaultdict(set)
    tree_loc = defaultdict(int)
    for path in paths:
        parts = path.split("/")
        stem = posixpath.splitext(parts[-1])[0].lower()
        for depth in range(1, len(parts)):
            directory = "/".join(parts[:depth])
            tree_loc[directory] += loc[path]
            if stem not in _GENERIC_STEMS:
                names[directory].add(stem)
    copied = defaultdict(int)
    for pair in pairs:
        trees = _sibling_trees(pair["a"]["path"], pair["b"]["path"])
        if trees:
            copied[trees] += pair["tokens"]
    siblings = defaultdict(list)
    for directory in names:
        siblings[posixpath.dirname(directory)].append(directory)

    rows = []
    for group in siblings.values():
        for a, b in combinations(sorted(group), 2):
            smaller = min(len(names[a]), len(names[b]))
            shared = names[a] & names[b]
            ratio = len(shared) / smaller if smaller else 0
            alike = smaller >= MIN_TREE_FILES and len(shared) >= MIN_SHARED_NAMES and (
                ratio >= MIN_SHARED_RATIO or len(shared) >= MIN_SHARED_NAMES_ANY_RATIO)
            if alike or copied[(a, b)] >= MIN_TREE_COPIED_TOKENS:
                rows.append({
                    "a": a, "b": b, "loc_a": tree_loc[a], "loc_b": tree_loc[b],
                    "shared_names": len(shared), "shared_ratio": round(ratio, 2),
                    "copied_tokens": copied[(a, b)],
                    "sample": sorted(shared)[:12],
                    "cochange": tree_cochange(commits, a, b),
                })
    rows.sort(key=lambda r: (-r["copied_tokens"], -r["shared_names"] * min(r["loc_a"], r["loc_b"])))
    return rows[:limit]


def _sibling_trees(path_a, path_b):
    """The two sibling directories under which two files diverge, or None if either sits directly
    in their common directory."""
    parts_a, parts_b = path_a.split("/")[:-1], path_b.split("/")[:-1]
    depth = 0
    while depth < min(len(parts_a), len(parts_b)) and parts_a[depth] == parts_b[depth]:
        depth += 1
    if depth >= len(parts_a) or depth >= len(parts_b):
        return None
    return tuple(sorted(("/".join(parts_a[:depth + 1]), "/".join(parts_b[:depth + 1]))))


# -- internals ------------------------------------------------------------------------------

def _tokenize(text, language, kinds_for, vocab):
    """Token ids and the line number of each token, comments and import lines left out."""
    ids, lines = array("i"), array("i")
    kinds = kinds_for(text, language)
    for number, (line, kind) in enumerate(zip(text.splitlines(), kinds), 1):
        if kind != "code" or _SKIP_LINE.match(line):
            continue
        code = _BLOCK_COMMENT_INLINE.sub(" ", line)
        for match in _TOKEN.finditer(code):
            token = match.group()
            if token[0] in "\"'`":
                token = "S"
            elif token[0].isdigit():
                token = "N"
            elif any(code.startswith(prefix, match.start()) for prefix in language.line_comments):
                break  # a trailing comment (string literals were matched whole, so this is outside them)
            ids.append(vocab.setdefault(token, len(vocab)))
            lines.append(number)
    return ids, lines


def _winnow(ids):
    """(fingerprint, token position) pairs chosen by winnowing, rightmost minimum per window."""
    n = len(ids)
    if n < K + WINDOW - 1:
        return []
    power = pow(_BASE, K - 1, _MOD)
    value = 0
    for token in ids[:K]:
        value = (value * _BASE + token) % _MOD
    hashes = [value]
    for i in range(K, n):
        value = ((value - ids[i - K] * power) * _BASE + ids[i]) % _MOD
        hashes.append(value)
    chosen, window, last = [], deque(), -1
    for i, value in enumerate(hashes):
        while window and hashes[window[-1]] >= value:
            window.pop()
        window.append(i)
        if window[0] <= i - WINDOW:
            window.popleft()
        if i >= WINDOW - 1 and window[0] != last:
            last = window[0]
            chosen.append((hashes[last], last))
    return chosen


def _extend(a, pa, b, pb, same_file):
    """Grow a shared k-gram into the maximal identical run. None on a hash collision."""
    if a[pa:pa + K] != b[pb:pb + K]:
        return None
    back = 0
    while pa - back > 0 and pb - back > 0 and a[pa - back - 1] == b[pb - back - 1]:
        back += 1
    ahead = K
    while pa + ahead < len(a) and pb + ahead < len(b) and a[pa + ahead] == b[pb + ahead]:
        ahead += 1
    sa, ea, sb, eb = pa - back, pa + ahead, pb - back, pb + ahead
    if same_file and ea > sb:  # a periodic run overlapping itself: keep the non-overlapping part
        length = sb - sa
        ea, eb = sa + length, sb + length
    return sa, ea, sb, eb


def _worth_reporting(stream_a, stream_b, region):
    sa, ea, sb, eb = region
    ids_a, lines_a = stream_a
    lines_b = stream_b[1]
    return (
        ea - sa >= MIN_TOKENS
        and len(set(ids_a[sa:ea])) >= MIN_DISTINCT
        and lines_a[ea - 1] - lines_a[sa] + 1 >= MIN_LINES
        and lines_b[eb - 1] - lines_b[sb] + 1 >= MIN_LINES
    )


def _merge(found):
    """Join copies of one block that were split by small edits. Yields (sa, ea, sb, eb, matched)."""
    merged = []
    for sa, ea, sb, eb in sorted(found):
        if merged:
            psa, pea, psb, peb, matched = merged[-1]
            if 0 <= sa - pea <= MERGE_GAP and 0 <= sb - peb <= MERGE_GAP:
                merged[-1] = (psa, ea, psb, eb, matched + ea - sa)
                continue
            if sa < pea:  # overlaps the previous copy (another alignment of the same text)
                continue
        merged.append((sa, ea, sb, eb, ea - sa))
    return merged


def _as_seen_from(path, pair):
    here, there = (pair["a"], pair["b"]) if pair["a"]["path"] == path else (pair["b"], pair["a"])
    return {
        "lines": [here["start_line"], here["end_line"]],
        "copy_of": f"{there['path']}:{there['start_line']}-{there['end_line']}",
        "tokens": pair["tokens"],
    }

"""Static evidence for "do these belong together?", usable from the first commit.

For each file we find its definitions (top-level ones, or the members of its single class),
then connect two definitions when one references the other or both touch the same
`self.x`/`this.x` state. The connected groups are the file's clusters (an LCOM4-style
measure). For top-level clusters we also look at who uses them: clusters that share their
callers are used together and belong together; clusters with separate callers are a real
seam. All of this is regex-level and approximate, and it is labelled as a hint.
"""

import re
from collections import defaultdict

from metrics import END_KEYWORD_LANGUAGES, brace_block_end

MIN_DEFINITIONS = 4
MAX_CLUSTERS = 6
MAX_SAMPLE = 6
MIN_CLIENT_NAME_LENGTH = 5  # shorter names match too many unrelated identifiers
_IDENTIFIER = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_SELF_FIELD = re.compile(r"\b(?:self|this|@)\.?([A-Za-z_][A-Za-z0-9_]*)")
_CLASS_START = re.compile(r"^\s*(?:export\s+)?(?:(?:public|private|protected|internal|abstract|final|sealed|static|data|open)\s+)*"
                          r"(?:class|struct|interface|trait|object|enum|impl)\s+([A-Za-z_]\w*)")
_NAME_AFTER_KEYWORD = re.compile(
    r"\bfunc\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)"  # Go, with an optional method receiver
    r"|\b(?:def|fn|function|fun|sub)\s+(?:<[^>]*>\s*)?(?:[A-Za-z_]\w*\.)?([A-Za-z_]\w*)"
)
_NAME_BEFORE_PAREN = re.compile(r"([A-Za-z_]\w*)\s*(?:=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>|\()")
_KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "function", "new", "else", "async", "await"}


def identifier_index(texts):
    """Map identifier -> set of paths whose text mentions it."""
    index = defaultdict(set)
    for path, text in texts.items():
        for name in set(_IDENTIFIER.findall(text)):
            index[name].add(path)
    return index


def analyze(path, text, language, index, importers):
    """Return a structure summary for one file, or None when there is nothing to say."""
    lines = text.splitlines()
    definitions = _definitions(lines, language)
    if not definitions:
        return None
    # Top level = not inside another definition, whatever the indentation (C++ namespaces indent
    # free functions; a file may mix those with a few at column 0).
    top = _outermost(definitions)
    level, members = "top-level", top
    if len(top) == 1 and top[0]["is_class"]:
        inner = [d for d in definitions if top[0]["start"] < d["start"] <= top[0]["end"]]
        if inner:
            members = _outermost(inner)
            level = f"members of {top[0]['name']}"
    if len(members) < MIN_DEFINITIONS:
        return None

    clusters = _clusters(members, lines)
    connected = [c for c in clusters if len(c) > 1][:MAX_CLUSTERS]
    isolated = [c[0] for c in clusters if len(c) == 1]
    summary = {
        "level": level,
        "definitions": len(members),
        "clusters": [{"size": len(c), "sample": c[:MAX_SAMPLE]} for c in connected],
        "isolated": {"count": len(isolated), "sample": isolated[:MAX_SAMPLE]},
    }
    if level == "top-level" and len(connected) > 1:
        clients = [_clients(c, path, index, importers) for c in connected]
        summary["cluster_clients"] = [len(c) for c in clients]
        users = [c for c in clients if c]
        if len(users) > 1:
            union, shared = set().union(*users), set.intersection(*users)
            summary["shared_client_ratio"] = round(len(shared) / len(union), 2)
    return summary


def _definitions(lines, language):
    """Definitions with name, indent and line span: to the matching closing brace in brace
    languages, else until the next definition at the same or lower indent."""
    found = []
    for number, line in enumerate(lines):
        is_class = bool(_CLASS_START.match(line))
        if not is_class and not (language.function_start and language.function_start.search(line)):
            continue
        name = _name(line, is_class)
        if name:
            indent = len(line.expandtabs(4)) - len(line.expandtabs(4).lstrip())
            found.append({"name": name, "indent": indent, "start": number, "is_class": is_class})
    for position, definition in enumerate(found):
        later = (d["start"] for d in found[position + 1:] if d["indent"] <= definition["indent"])
        definition["end"] = next(later, len(lines)) - 1
        if not language.indent_scoped and language.name not in END_KEYWORD_LANGUAGES:
            braced = brace_block_end(lines, definition["start"])
            if braced is not None:
                definition["end"] = braced
    return found


def _outermost(definitions):
    """Definitions not nested inside another one (they arrive in line order)."""
    outermost, reach = [], -1
    for definition in definitions:
        if definition["start"] > reach:
            outermost.append(definition)
            reach = definition["end"]
        else:
            reach = max(reach, definition["end"])
    return outermost


def _name(line, is_class):
    if is_class:
        return _CLASS_START.match(line).group(1)
    match = _NAME_AFTER_KEYWORD.search(line)
    if match:
        return match.group(1) or match.group(2)
    for match in _NAME_BEFORE_PAREN.finditer(line):
        if match.group(1) not in _KEYWORDS:
            return match.group(1)
    return None


def _clusters(members, lines):
    """Union definitions that reference each other or share instance state; largest group first."""
    parent = list(range(len(members)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    names = {m["name"]: i for i, m in enumerate(members)}
    field_users = defaultdict(set)
    for i, member in enumerate(members):
        body = "\n".join(lines[member["start"] + 1:member["end"] + 1])
        for token in set(_IDENTIFIER.findall(body)):
            j = names.get(token)
            if j is not None and j != i:
                parent[find(i)] = find(j)
        for field in set(_SELF_FIELD.findall(body)):
            field_users[field].add(i)
    for users in field_users.values():
        first, *rest = sorted(users)
        for other in rest:
            parent[find(other)] = find(first)

    groups = defaultdict(list)
    for i, member in enumerate(members):
        groups[find(i)].append(member["name"])
    return sorted(groups.values(), key=len, reverse=True)


def _clients(names, path, index, importers):
    """Files that mention any of these names; limited to importers when imports resolved."""
    users = set()
    for name in names:
        if len(name) >= MIN_CLIENT_NAME_LENGTH:
            users |= index.get(name, set())
    users.discard(path)
    return users & importers if importers else users

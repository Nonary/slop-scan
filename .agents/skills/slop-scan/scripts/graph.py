"""Dependency graph facts: fan-in/out, module-level edges, instability, and import cycles."""

import posixpath
from collections import Counter, defaultdict

C_IMPLEMENTATIONS = (".c", ".cc", ".cpp", ".cxx", ".m", ".mm")
C_HEADERS = (".h", ".hh", ".hpp", ".hxx")


def module_of(path):
    return posixpath.dirname(path) or "."


def companion_headers(file_edges):
    """Map each C-family implementation file to the header that declares its interface.

    Nothing ever includes a .cpp, so its importers are really its header's importers. The
    companion is a same-named header the file includes itself, else one in the same directory.
    """
    headers = defaultdict(list)
    for path in file_edges:
        if path.endswith(C_HEADERS):
            headers[_stem(path)].append(path)
    companions = {}
    for path, targets in file_edges.items():
        if not path.endswith(C_IMPLEMENTATIONS):
            continue
        candidates = headers.get(_stem(path), [])
        included = [h for h in candidates if h in targets]
        beside = [h for h in candidates if posixpath.dirname(h) == posixpath.dirname(path)]
        pick = included or beside
        if len(pick) == 1:
            companions[path] = pick[0]
    return companions


def importers_of(path, graph, companions):
    """Files that depend on `path`: its direct importers, plus its header's for an implementation file."""
    users = set(graph["imported_by"][path])
    header = companions.get(path)
    if header:
        users |= set(graph["imported_by"][header])
        users.discard(path)
    return users


def _stem(path):
    return posixpath.splitext(posixpath.basename(path))[0]


def build_graph(file_edges):
    """file_edges: {path: set(imported paths)} covering every scanned file."""
    imported_by = defaultdict(set)
    for source, targets in file_edges.items():
        for target in targets:
            imported_by[target].add(source)

    module_edges = Counter()
    for source, targets in file_edges.items():
        for target in targets:
            if module_of(source) != module_of(target):
                module_edges[(module_of(source), module_of(target))] += 1

    module_graph = defaultdict(set)
    for source, target in module_edges:
        module_graph[source].add(target)

    return {
        "imports": {path: sorted(targets) for path, targets in file_edges.items()},
        "imported_by": {path: sorted(imported_by.get(path, ())) for path in file_edges},
        "module_edges": [
            {"from": source, "to": target, "count": count}
            for (source, target), count in sorted(module_edges.items())
        ],
        "modules": _module_stats(file_edges, module_edges),
        "file_cycles": _cycles(file_edges),
        "module_cycles": _cycles(module_graph),
    }


def _module_stats(file_edges, module_edges):
    efferent, afferent = defaultdict(set), defaultdict(set)
    for source, target in module_edges:
        efferent[source].add(target)
        afferent[target].add(source)
    stats = {}
    for module in sorted({module_of(path) for path in file_edges}):
        ce, ca = len(efferent[module]), len(afferent[module])
        stats[module] = {
            "depends_on": sorted(efferent[module]),
            "depended_on_by": sorted(afferent[module]),
            "instability": round(ce / (ca + ce), 2) if ca + ce else None,
        }
    return stats


def _cycles(graph):
    """Strongly connected components with more than one node (iterative Tarjan)."""
    index_of, lowlink, on_stack = {}, {}, set()
    stack, components, counter = [], [], 0
    nodes = set(graph) | {t for targets in graph.values() for t in targets}

    for root in sorted(nodes):
        if root in index_of:
            continue
        work = [(root, iter(sorted(graph.get(root, ()))))]
        index_of[root] = lowlink[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, children = work[-1]
            child = next(children, None)
            if child is not None:
                if child not in index_of:
                    index_of[child] = lowlink[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(graph.get(child, ())))))
                elif child in on_stack:
                    lowlink[node] = min(lowlink[node], index_of[child])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
            if lowlink[node] == index_of[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    components.append(sorted(component))
    return sorted(components, key=len, reverse=True)

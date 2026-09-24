"""Tier 0: what actually runs.

Entry points come from what is checked in: container, compose, CI and process files, Makefiles,
package.json, pyproject and setup files, shell scripts, the files a framework runs by convention
(Next, Nuxt, SvelteKit, Remix and Astro routes, tool configs, Django, Celery, Airflow and Ansible
plugins), and files that start themselves (`if __name__ == "__main__"`, a shebang). Following
imports from them gives every Python and JavaScript-family file a `reach`, the strongest kind of
entry point that reaches it:

  launched  something checked in starts it, or a framework runs it by convention
  manual    only entry points that nothing checked in starts reach it (`reach_roots` names them)
  tests     only tests and stories reach it
  none      nothing reaches it

Docs are not entry points: they say what code is meant to do, configuration says what runs.
Loads the script can see count as imports: a dotted module name or a source path in another
file's string literals, a package that imports its own directory, and bundler globs. A language
with no launched code at all (a CLI with no packaging) counts its self-starting files as launched,
and a Python package with no launcher counts its public modules as launched (its users import them).

Only Python and the JavaScript family are analyzed, because only there does a missing import mean
the code is unused: a C or C++ build links what its build files list, and JVM and .NET code uses
its own package without imports. Those files get `reach: null`.
"""

import fnmatch
import json
import posixpath
import re
from collections import Counter, defaultdict

from graph import module_of
from imports import ImportResolver

FAMILIES = {"python": "python", "javascript": "js", "typescript": "js", "vue": "js", "svelte": "js"}
KINDS = ("launched", "manual", "tests")  # strongest first; a file none of them reaches is "none"
MAX_CONFIG_BYTES = 256_000
MAX_LAUNCHERS = 60
MAX_ROOTS_PER_FILE = 3

# Files that start code: every source path, module name and command in them counts.
_RUN_CONFIG = re.compile(
    r"(?:^|/)(?:(?:docker|container)file[^/]*|[^/]*\.(?:docker|container)file|[^/]*compose[^/]*\.ya?ml"
    r"|procfile[^/]*|(?:gnu)?makefile|[^/]*\.mk|justfile|taskfile[^/]*\.ya?ml|jenkinsfile|tiltfile|doxyfile[^/]*"
    r"|\.gitlab-ci\.ya?ml|bitbucket-pipelines\.ya?ml|azure-pipelines[^/]*\.ya?ml|wrangler\.(?:toml|jsonc?)"
    r"|vercel\.json|netlify\.toml|fly\.toml|render\.ya?ml|app\.ya?ml|nixpacks\.toml|[^/]*\.service"
    r"|supervisord?[^/]*\.conf|[^/]*\.html?|[tj]sconfig[^/]*\.json)$"
    r"|(?:^|/)\.(?:github/workflows|circleci)/[^/]+\.ya?ml$",
    re.IGNORECASE,
)
# Files that name entry points among much else (lint settings, file lists): only explicit
# `pkg.mod:function` targets, `python -m` and a runner followed by a path count.
_ENTRY_CONFIG = re.compile(r"(?:^|/)(?:pyproject\.toml|setup\.cfg|setup\.py|tox\.ini)$|\.(?:ya?ml|toml|ini|cfg|conf)$",
                           re.IGNORECASE)
_NOT_CONFIG = re.compile(r"(?:^|/)(?:[^/]*lock\.ya?ml|[^/]*\.lock|\.pre-commit-config\.ya?ml|mkdocs\.ya?ml)$", re.IGNORECASE)
_PACKAGING = ("pyproject.toml", "setup.py", "setup.cfg")

_UNQUOTE = re.compile(r"""["']\s*,\s*["']""")  # ["python", "-m", "app"] reads as `python -m app`
_PY_MODULE_RUN = re.compile(r"\bpython[\d.]*(?:\s+-[A-Za-z]+)*\s+-m\s+([A-Za-z_][\w.]*)")
_APP_SERVER = re.compile(
    r"\b(?:uvicorn|gunicorn|hypercorn|daphne|granian|waitress-serve|celery|faust|flask|rq|arq|dramatiq)\b[^\n;&|]*?"
    r"(?:(?:-A|--app)[\s=]+([A-Za-z_][\w.]*)|(?<![\w./-])([A-Za-z_][\w.]*):[A-Za-z_]\w*)")
_ENTRY_TARGET = re.compile(r"(?<![\w./:-])([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+):[A-Za-z_]\w*")  # pkg.mod:main
_RUNNER_PATH = re.compile(r"\b(?:python[\d.]*|node|bun|deno\s+run|tsx|ts-node|sh|bash)\s+(?:-\S+\s+)*([\w./@-]+)")
_DOTTED = re.compile(r"(?<![\w./:@-])[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+")
_PATH = re.compile(r"(?<![\w@$.-])(?:\.{1,2}/|/)?(?:[\w@.-]+/)*[\w@-][\w@.-]*\.(?:pyw?|[cm]?jsx?|[cm]?tsx?|vue|svelte)\b")
# One-line string literals. A quote next to another quote is a docstring or an empty string.
_STRING = re.compile(r"""(?<!["'`])(["'`])((?:\\.|(?!\1)[^\\\n]){3,300})\1(?!["'`])""")
# A path built from pieces: path.join(__dirname, "src", "main.js"), Path(__file__).parent / "sub" / "x.py".
_JOINED = re.compile(r"""["'][\w.@-]+["'](?:\s*[,/]\s*["'][\w.@-]+["'])+""")
_PIECE = re.compile(r"""["']([\w.@-]+)["']""")

_PY_MAIN = re.compile(r"""^if\s+__name__\s*==\s*["']__main__["']\s*:""", re.MULTILINE)
_PY_SELF_LOAD = re.compile(
    r"\bpkgutil\.(?:iter_modules|walk_packages)\b|\b(?:import_module|__import__)\(\s*(?:f[\"']|[\"']\.|[\w.]*__(?:name|package)__)")
_JS_GLOB = re.compile(r"""import\.meta\.glob(?:Eager)?\(\s*\[?\s*["'`]([^"'`]+)["'`]""")
_JS_CONTEXT = re.compile(r"""require\.context\(\s*["'`]([^"'`]+)["'`]""")

_JS_EXT = r"\.(?:[cm]?jsx?|[cm]?tsx?|vue|svelte|mdx)$"
_ROUTE_FILES = (r"(?:page|layout|route|loading|error|global-error|not-found|template|default|forbidden|unauthorized"
                r"|opengraph-image|twitter-image|icon|apple-icon|sitemap|robots|manifest)")
# Files a framework loads by convention, relative to the directory holding its package.json.
_FRAMEWORK_FILES = {
    "next": (rf"^(?:src/)?app/(?:.*/)?{_ROUTE_FILES}{_JS_EXT}", rf"^(?:src/)?pages/.*{_JS_EXT}",
             rf"^(?:src/)?(?:middleware|instrumentation(?:-client)?){_JS_EXT}"),
    "nuxt": (rf"^(?:app/)?(?:pages|layouts|middleware|plugins|components|composables|utils|server)/.*{_JS_EXT}",
             rf"^(?:app/)?(?:app|error|app\.config){_JS_EXT}"),
    "sveltekit": (rf"^src/routes/(?:.*/)?\+[\w.-]+{_JS_EXT}", rf"^src/(?:hooks(?:\.\w+)?|service-worker){_JS_EXT}",
                  rf"^src/params/.*{_JS_EXT}"),
    "remix": (rf"^app/(?:root|routes|entry\.\w+){_JS_EXT}", rf"^app/routes/.*{_JS_EXT}"),
    "astro": (rf"^src/pages/.*{_JS_EXT}", rf"^src/(?:middleware|content/config|content\.config){_JS_EXT}"),
    "expo-router": (rf"^(?:src/)?app/.*{_JS_EXT}",),
    "tanstack": (rf"^(?:src/|app/)?routes/.*{_JS_EXT}", rf"^(?:src/|app/)?(?:router|client|server|start|ssr){_JS_EXT}"),
    "nitro": (rf"^(?:src/)?server/(?:routes|api|middleware|plugins|tasks)/.*{_JS_EXT}",),
}
_FRAMEWORK_DEPS = {
    "next": ("next",), "vinext": ("next",), "nuxt": ("nuxt",), "@sveltejs/kit": ("sveltekit",),
    "@remix-run/react": ("remix",), "@react-router/dev": ("remix",), "astro": ("astro",), "expo-router": ("expo-router",),
    "@tanstack/react-start": ("tanstack", "nitro"), "@tanstack/solid-start": ("tanstack", "nitro"),
    "@tanstack/start": ("tanstack", "nitro"), "@tanstack/router-plugin": ("tanstack",),
    "@tanstack/router-vite-plugin": ("tanstack",), "nitropack": ("nitro",), "nitro": ("nitro",), "vinxi": ("nitro",),
}
_JS_CONVENTIONAL_ENTRY = re.compile(rf"^(?:src/)?(?:main|index){_JS_EXT}")
_TOOL_CONFIG = re.compile(r"(?:^|/)(?:[\w.-]+\.config|\.[\w-]+rc)\.[cm]?[jt]s$")
_PY_CONVENTIONS = {  # (marker import, path pattern)
    "django": (r"^\s*(?:from|import)\s+django\b",
               r"(?:^|/)(?:manage|models|admin|apps|urls|signals|wsgi|asgi|settings)\.py$|/(?:management/commands|templatetags)/[^/]+\.py$"),
    "celery": (r"^\s*(?:from|import)\s+celery\b", r"(?:^|/)tasks\.py$"),
    "airflow": (r"^\s*(?:from|import)\s+airflow\b", r"(?:^|/)dags/.+\.py$"),
}
_PY_TOOL_FILE = re.compile(r"(?:^|/)(?:setup|noxfile|fabfile|dodo)\.py$|^tasks\.py$|(?:^|/)docs?/(?:source/)?conf\.py$")
_ANSIBLE_PLUGIN = re.compile(
    r"(?:^|/)plugins/(?:modules|module_utils|action|filter|lookup|callback|inventory|connection|test)/[^/]+\.py$"
    r"|(?:^|/)library/[^/]+\.py$")
_TEST_SUPPORT = re.compile(r"(?:^|/)(?:conftest\.py|__mocks__/.*|\.storybook/.*|[^/]*\.stories\.[^/]+"
                           r"|(?:setupTests|test-setup|vitest\.setup|jest\.setup)\.[^/]+)$")
_BUILD_DIRS = ("dist", "build", "lib", "out", "esm", "cjs", "es", "umd", "public", "static")
_BUILT_JS = re.compile(r"\.[cm]?js$")


def analyze(paths, sources, files, file_edges, resolver, read_text, generated=None):
    """paths: every repository path inventory considered, source or not. sources: {path:
    (language, text, is_test)}. files: manifest entries. file_edges: {path: imported paths}.
    read_text(path) returns a repository file's text or None. generated: {path: (language, text)}
    for generated files the scan skips; imports pass through them (a generated route tree).

    Returns ({path: {"reach", "reach_roots"?}}, summary)."""
    if generated:
        file_edges = _edges_through_generated(sources, generated, read_text)
    family = {path: FAMILIES.get(language.name) for path, (language, _, _) in sources.items()}
    js_projects = {posixpath.dirname(p) for p in paths if posixpath.basename(p).lower() == "package.json"}
    mentions = _Mentions(sources, resolver, js_projects)
    edges = defaultdict(set)
    for path, (language, text, _) in sources.items():
        edges[path] |= set(file_edges.get(path, ()))
        if family[path]:
            edges[path] |= mentions.loads(path, language.name, text)
        if family[path] == "python":
            edges[path] |= _package_inits(path, sources)
        edges[path].discard(path)

    roots = _Roots(sources, family)
    packages = _launch_from_configs(roots, paths, sources, mentions, read_text)
    _launch_by_convention(roots, sources, family, packages)
    _launch_fallbacks(roots, paths, sources, family)
    status, manual_roots = _trace(roots, edges)

    result = {}
    for path in sources:
        if not family[path]:
            result[path] = {"reach": None}
            continue
        result[path] = {"reach": status.get(path, "none")}
        if result[path]["reach"] == "manual":
            result[path]["reach_roots"] = sorted(manual_roots[path])[:MAX_ROOTS_PER_FILE]
    return result, _summary(files, family, result, manual_roots, roots)


class _Roots:
    """The entry points found so far. Tests are their own entry points, and nothing reached only
    through a test counts as run, even when a checked-in script starts the test."""

    def __init__(self, sources, family):
        self.family = family
        self.tests = {p for p, (_, _, is_test) in sources.items() if family[p] and (is_test or _TEST_SUPPORT.search(p))}
        self.launched, self.launchers, self.conventions = {}, [], Counter()
        self.manual, self.library, self.self_started = set(), [], []

    def launch(self, path, config, how=None):
        """Record a launched entry point; returns whether it is new. `how` lists it in the report."""
        if not self.family.get(path) or path in self.launched or path in self.tests:
            return False
        self.launched[path] = config
        if how:
            self.launchers.append({"config": config, "path": path, "how": how[:100]})
        return True

    def has_launched(self, family):
        return any(self.family[p] == family for p in self.launched)


def _launch_from_configs(roots, paths, sources, mentions, read_text):
    """Entry points named by checked-in configuration and scripts. Returns {directory: package.json}."""
    packages = {}
    for path in paths:
        if posixpath.basename(path).lower() == "package.json":
            packages[posixpath.dirname(path)] = _loose_json(read_text(path)) or {}
            continue
        if _NOT_CONFIG.search(path):
            continue
        is_shell = path in sources and sources[path][0].name == "shell"
        careful = not (is_shell or _RUN_CONFIG.search(path))
        if careful and not _ENTRY_CONFIG.search(path):
            continue
        text = sources[path][1] if path in sources else read_text(path)
        if text and len(text) <= MAX_CONFIG_BYTES:
            for target, how in mentions.launched_by(path, text, careful):
                roots.launch(target, path, how)
    for directory, package in packages.items():
        config = posixpath.join(directory, "package.json") if directory else "package.json"
        scripts = package.get("scripts")
        for command in scripts.values() if isinstance(scripts, dict) else ():
            if isinstance(command, str):
                for target, how in mentions.launched_by(config, command, careful=False):
                    roots.launch(target, config, how)
        for entry in _package_entries(package):
            roots.launch(mentions.built_file(directory, entry), config, f"package entry {entry}")
    return packages


def _launch_by_convention(roots, sources, family, packages):
    """Files a framework or tool runs by convention, and files that can start themselves."""
    frameworks = {directory: sorted({f for d in _dependencies(package) for f in _FRAMEWORK_DEPS.get(d, ())})
                  for directory, package in packages.items()}
    python_frameworks = {
        name for name, (marker, _) in _PY_CONVENTIONS.items()
        if any(family[p] == "python" and re.search(marker, text, re.MULTILINE) for p, (_, text, _) in sources.items())
    }
    for path, (_, text, _) in sources.items():
        if not family[path] or path in roots.tests:
            continue
        convention = _convention(path, family[path], frameworks, python_frameworks)
        if convention and roots.launch(path, convention):
            roots.conventions[convention] += 1
        if text.startswith("#!") or (family[path] == "python" and (_PY_MAIN.search(text) or path.endswith("__main__.py"))):
            roots.manual.add(path)


def _launch_fallbacks(roots, paths, sources, family):
    """A language with nothing launched: a packaged Python library launches its public modules,
    and otherwise files that start themselves are what runs."""
    for name in ("python", "js"):
        if roots.has_launched(name):
            continue
        if name == "python":
            roots.library = [p for p in _public_package_modules(paths, sources, family) if roots.launch(p, "package API")]
        if not roots.has_launched(name) and any(family[p] == name for p in roots.manual):
            roots.self_started.append(name)
            for path in sorted(p for p in roots.manual if family[p] == name):
                roots.launch(path, "runs itself", "no checked-in launcher for this language")


def _trace(roots, edges):
    """Each file's strongest kind of entry point, and the manual entry points that reach it."""
    status = {path: "tests" for path in roots.tests}
    for path in _walk(roots.launched, edges, roots.tests):
        status[path] = "launched"
    manual_roots = defaultdict(set)
    for root in sorted(roots.manual - set(status)):
        for path in _walk([root], edges, roots.tests):
            if status.setdefault(path, "manual") == "manual":
                manual_roots[path].add(root)
    for path in _walk(roots.tests, edges):
        status.setdefault(path, "tests")
    return status, manual_roots


class _Mentions:
    """Finds the source files a piece of text names: by path, by dotted module, or by command."""

    def __init__(self, sources, resolver, js_projects):
        self.sources = sources
        self.resolver = resolver
        self.js_projects = js_projects
        self.suffixes = defaultdict(set)  # "js/app.js" -> files ending in /js/app.js
        for path in sources:
            parts = path.split("/")
            for start in range(1, len(parts) - 1):
                self.suffixes["/".join(parts[start:])].add(path)

    def launched_by(self, config, text, careful):
        """(file, how) pairs a config or script starts. `careful` accepts only explicit commands."""
        text = _UNQUOTE.sub(" ", text)
        here = posixpath.dirname(config)
        for match in _PY_MODULE_RUN.finditer(text):
            yield self.resolver.python_module(match.group(1), min_parts=1), match.group(0)
        for match in _APP_SERVER.finditer(text):
            yield self.resolver.python_module(match.group(1) or match.group(2), min_parts=1), match.group(0)
        for match in _ENTRY_TARGET.finditer(text):
            yield self.resolver.python_module(match.group(1)), match.group(0)
        for match in _RUNNER_PATH.finditer(text):
            yield self.file(match.group(1), here, loose=False), match.group(0)
        if careful:
            return
        for match in _PATH.finditer(text):
            yield self.file(match.group(0), here), match.group(0)
        for match in _DOTTED.finditer(text):
            yield self.resolver.python_module(match.group(0)), match.group(0)

    def loads(self, path, language, text):
        """Files `path` loads without an import statement the resolver understands."""
        here = posixpath.dirname(path)
        found = set()
        for match in _JOINED.finditer(text):
            found.update(self.file(token, here) for token in _PATH.findall("/".join(_PIECE.findall(match.group(0)))))
        for match in _STRING.finditer(text):
            literal = match.group(2)
            found.update(self.file(token, here) for token in _PATH.findall(literal))
            if language == "python":
                found.update(self.resolver.python_module(token) for token in _DOTTED.findall(literal))
                found.update(self.resolver.python_module(m.group(1)) for m in _ENTRY_TARGET.finditer(literal))
        if language == "python" and _PY_SELF_LOAD.search(text):
            found.update(p for p in self.sources if p.startswith(f"{here}/" if here else "") and p.endswith(".py"))
        for pattern in _JS_GLOB.findall(text):
            base = pattern.lstrip("/") if pattern.startswith("/") else posixpath.normpath(posixpath.join(here, pattern))
            found.update(p for p in self.sources if fnmatch.fnmatch(p, base))
        for directory in _JS_CONTEXT.findall(text):
            base = posixpath.normpath(posixpath.join(here, directory))
            found.update(p for p in self.sources if p.startswith(base + "/"))
        found.discard(None)
        found.discard(path)
        return found

    def file(self, token, here, loose=True):
        """The scanned file a path token names, relative to the naming file's directory or the
        repository root; `loose` also accepts a unique match on a path's last two or more parts."""
        token = token.strip().rstrip(".,;:")
        if not token or "://" in token:
            return None
        for base in ([posixpath.join(here, token)] if token.startswith(".") else
                     [posixpath.join(here, token.lstrip("/")), token.lstrip("/")]):
            base = posixpath.normpath(base)
            hit = base not in (".", "") and not base.startswith("../") and self.resolver.source_file(base)
            if hit:
                return hit
        if loose and "/" in token.strip("./"):
            hits = self.suffixes.get(posixpath.normpath(token.lstrip("./")), ())
            if len(hits) == 1:
                return next(iter(hits))
        project = _nearest(posixpath.join(here, "x"), self.js_projects)
        if project is not None and _BUILT_JS.search(token):
            return self.built_file(project, token)
        return None

    def built_file(self, directory, entry):
        """The source behind a compiled JavaScript path in project `directory` (a package.json
        entry, an HTML script tag): the file itself, else its original under src/ or the project,
        with any build directory (dist/, build/, ...) taken off the front."""
        entry = posixpath.normpath(entry.lstrip("./")).lstrip("/")
        head, _, rest = entry.partition("/")
        rest = rest if head in _BUILD_DIRS and rest else entry
        for base in (entry, f"src/{rest}", rest):
            hit = self.resolver.source_file(posixpath.normpath(posixpath.join(directory, base)))
            if hit:
                return hit
        return None


def _edges_through_generated(sources, generated, read_text):
    """The import graph re-resolved with generated files as nodes."""
    texts = {path: (language, text) for path, (language, text, _) in sources.items()}
    texts.update(generated)
    resolver = ImportResolver(texts, read_text)
    return {path: resolver.resolve(path, language.name, text)[0] for path, (language, text) in texts.items()}


def _convention(path, family, frameworks, python_frameworks):
    """What runs this file by convention, if anything."""
    if family == "js":
        if _TOOL_CONFIG.search(path):
            return "tool config"
        project = _nearest(path, frameworks)
        if project is None:
            return None
        relative = path[len(project) + 1:] if project else path
        for name in frameworks[project]:
            if any(re.search(pattern, relative) for pattern in _FRAMEWORK_FILES[name]):
                return f"{name} ({project or '.'})"
        if _JS_CONVENTIONAL_ENTRY.search(relative):
            return f"entry file ({project or '.'})"
        return None
    if _PY_TOOL_FILE.search(path):
        return "tool config"
    if _ANSIBLE_PLUGIN.search(path):
        return "ansible plugin"
    for name in python_frameworks:
        if re.search(_PY_CONVENTIONS[name][1], path):
            return name
    return None


def _nearest(path, directories):
    """The deepest directory in `directories` that holds `path` ('' is the repository root)."""
    directory = posixpath.dirname(path)
    while True:
        if directory in directories:
            return directory
        if not directory:
            return None
        directory = posixpath.dirname(directory)


def _package_inits(path, sources):
    """Importing a module runs every package __init__ above it."""
    inits, directory = set(), posixpath.dirname(path)
    while directory:
        init = f"{directory}/__init__.py"
        if init in sources:
            inits.add(init)
        directory = posixpath.dirname(directory)
    return inits


def _public_package_modules(paths, sources, family):
    """Modules of a packaged Python library that its users may import: everything under a
    top-level package beside (or in src/ beside) pyproject/setup, except `_private` names."""
    projects = {posixpath.dirname(p) for p in paths if posixpath.basename(p) in _PACKAGING}
    tops = set()
    for path in sources:
        if path.endswith("/__init__.py") or path == "__init__.py":
            package = posixpath.dirname(path)
            parent = posixpath.dirname(package)
            if parent in projects or (posixpath.basename(parent) == "src" and posixpath.dirname(parent) in projects):
                tops.add(package)
    return sorted(
        path for path in sources
        if family[path] == "python" and not sources[path][2]
        and any(path.startswith(top + "/") for top in tops)
        and not any(part.startswith("_") and part != "__init__.py" for part in path.split("/"))
    )


def _package_entries(package):
    """Paths package.json exposes: main, module, browser, bin and every string under exports."""
    entries = [package.get(key) for key in ("main", "module", "browser")]
    bins = package.get("bin")
    entries += list(bins.values()) if isinstance(bins, dict) else [bins]
    stack = [package.get("exports")]
    while stack:
        value = stack.pop()
        if isinstance(value, str):
            entries.append(value)
        elif isinstance(value, dict):
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return [e for e in entries if isinstance(e, str) and "*" not in e]


def _dependencies(package):
    names = set()
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        if isinstance(package.get(key), dict):
            names |= set(package[key])
    return names


def _walk(starts, edges, avoid=()):
    """Everything reachable from `starts` without passing through `avoid`."""
    seen, stack = set(), [s for s in starts if s not in avoid]
    while stack:
        path = stack.pop()
        if path in seen:
            continue
        seen.add(path)
        stack.extend(t for t in edges.get(path, ()) if t not in avoid)
    return seen


def _summary(files, family, result, manual_roots, roots):
    loc = {entry["path"]: entry["loc"] for entry in files}
    code = [e["path"] for e in files if family.get(e["path"]) and not e["is_test"]]
    by_status = defaultdict(list)
    for path in code:
        by_status[result[path]["reach"]].append(path)
    reached_by = defaultdict(set)
    for path in by_status["manual"]:
        for root in manual_roots[path]:
            reached_by[root].add(path)
    return {
        "analyzed": {"files": len(code), "loc": sum(loc[p] for p in code)},
        "totals": {kind: {"files": len(by_status[kind]), "loc": sum(loc[p] for p in by_status[kind])}
                   for kind in (*KINDS, "none")},
        "launchers": sorted(roots.launchers, key=lambda l: (l["config"], l["path"]))[:MAX_LAUNCHERS],
        "conventions": dict(roots.conventions.most_common()),
        "self_started": roots.self_started,
        "library_modules": len(roots.library),
        "manual": _manual_groups(reached_by, loc),
        "tests_only": _by_module(by_status["tests"], loc),
        "unreached": _by_module(by_status["none"], loc),
    }


def _manual_groups(reached_by, loc):
    """Entry points nothing launches, merged when they share code, largest first."""
    groups = []
    for root, reached in sorted(reached_by.items()):
        merged = {"roots": {root}, "files": set(reached)}
        for group in [g for g in groups if g["files"] & merged["files"]]:
            groups.remove(group)
            merged["roots"] |= group["roots"]
            merged["files"] |= group["files"]
        groups.append(merged)
    rows = [{"roots": sorted(g["roots"]), "files": sorted(g["files"]), "loc": sum(loc[p] for p in g["files"])}
            for g in groups]
    return sorted(rows, key=lambda row: (-row["loc"], row["roots"]))


def _by_module(paths, loc):
    modules = defaultdict(list)
    for path in paths:
        modules[module_of(path)].append(path)
    rows = [{"module": module, "files": sorted(members), "loc": sum(loc[p] for p in members)}
            for module, members in modules.items()]
    return sorted(rows, key=lambda row: (-row["loc"], row["module"]))


def _loose_json(text):
    try:
        value = json.loads(text) if text else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None

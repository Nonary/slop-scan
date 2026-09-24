"""Best-effort extraction of in-repo import edges, one resolver per language family.

Unresolvable imports (third-party packages, dynamic loading, unsupported languages) are only
counted, never guessed: a missing edge is cheaper than a false one.
"""

import json
import posixpath
import re
from collections import defaultdict
from pathlib import PurePosixPath

_PY_IMPORT = re.compile(r"^\s*import\s+([\w.]+(?:\s*,\s*[\w.]+)*)", re.MULTILINE)
_PY_FROM = re.compile(r"^\s*from\s+(\.*[\w.]*)\s+import\s+(\([^)]*\)|[^\n#;]+)", re.MULTILINE)
_JS_SPEC = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*|\bexport\s+\*\s+from\s*)['"]([^'"]+)['"]"""
)
_GO_BLOCK = re.compile(r"^import\s*\(([^)]*)\)", re.MULTILINE | re.DOTALL)
_GO_SINGLE = re.compile(r'^import\s+(?:\w+\s+)?"([^"]+)"', re.MULTILINE)
_GO_QUOTED = re.compile(r'"([^"]+)"')
_GO_MODULE = re.compile(r"^module\s+(\S+)", re.MULTILINE)
_RUST_USE = re.compile(r"^\s*(?:pub\s+)?use\s+(crate|super|self)::([\w:]+)", re.MULTILINE)
_RUST_MOD = re.compile(r"^\s*(?:pub\s+)?mod\s+(\w+)\s*;", re.MULTILINE)
_JVM_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)", re.MULTILINE)
_C_INCLUDE = re.compile(r'^\s*#\s*(?:include|import)\s+"([^"]+)"', re.MULTILINE)
_RUBY_REQUIRE = re.compile(r"""^\s*require_relative\s+['"]([^'"]+)['"]""", re.MULTILINE)
_PHP_REQUIRE = re.compile(r"""\b(?:require|include)(?:_once)?\s*\(?\s*(?:__DIR__\s*\.\s*)?['"]([^'"]+)['"]""")

_JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".vue", ".svelte")
# Files that mark the root of a JS/TS project and may define import aliases.
_JS_BUNDLER_CONFIGS = tuple(f"{name}.config.{ext}" for name in ("vite", "vitest", "nuxt", "svelte", "webpack")
                            for ext in ("ts", "js", "mts", "mjs", "cjs"))
_JS_TS_CONFIGS = ("tsconfig.json", "tsconfig.app.json", "tsconfig.base.json", "jsconfig.json")
_JS_PROJECT_MARKERS = _JS_TS_CONFIGS + ("package.json",)
_ALIAS_BLOCK = re.compile(r"\balias\s*:\s*[\[{]", re.MULTILINE)
_ALIAS_ENTRY = re.compile(r"""['"]([@~#$][\w/-]*)['"]\s*:\s*([^\n]+)""")
_ALIAS_FIND = re.compile(r"""find\s*:\s*['"]([@~#$][\w/-]*)['"]\s*,\s*replacement\s*:\s*([^\n}]+)""")
_PATH_LITERAL = re.compile(r"""['"]((?:\.{1,2}|[\w@-]+)(?:/[\w.@-]*)*)['"]""")
_JSON_COMMENT = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/', re.DOTALL)
_JSON_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


class ImportResolver:
    def __init__(self, paths, read_text):
        self.paths = set(paths)
        self._read_text = read_text
        self._python_index = _python_module_index(self.paths)
        self._suffix_index = _suffix_index(self.paths)
        self._go_modules = self._find_go_modules()
        self._files_by_dir = defaultdict(list)
        for path in self.paths:
            self._files_by_dir[posixpath.dirname(path)].append(path)
        self._js_projects = {}  # directory -> nearest JS project root (or None)
        self._js_aliases = {}  # project root -> [(prefix, [base directories])]

    def resolve(self, path, language, text):
        """Return (in-repo targets, count of imports that stayed unresolved)."""
        handler = {
            "python": self._python,
            "javascript": self._javascript, "typescript": self._javascript,
            "vue": self._javascript, "svelte": self._javascript,
            "go": self._go,
            "rust": self._rust,
            "java": self._jvm, "kotlin": self._jvm, "scala": self._jvm, "groovy": self._jvm,
            "c": self._c, "cpp": self._c,
            "ruby": self._ruby,
            "php": self._php,
        }.get(language)
        if handler is None:
            return set(), 0
        targets, unresolved = set(), 0
        for found in handler(path, text):
            if found:
                targets.update(found)
            else:
                unresolved += 1
        targets.discard(path)
        return targets, unresolved

    # -- lookups for other tier-0 passes ----------------------------------------------------

    def python_module(self, dotted, min_parts=2):
        """The file a dotted name refers to: the module itself, or its longest prefix that is a
        module (`pkg.mod.Class` and `pkg.mod:main` both name pkg/mod.py). Prefixes stop at
        `min_parts` components, because a bare `logging` or `config` matches too much."""
        parts = dotted.split(".")
        for length in range(len(parts), min(min_parts, len(parts)) - 1, -1):
            hit = self._python_lookup(".".join(parts[:length]))
            if hit:
                return next(iter(hit))
        return None

    def source_file(self, base):
        """The scanned file at repo-relative `base`, trying JS-style extensions and index files."""
        hit = self._first_existing(_js_candidates(base))
        return next(iter(hit)) if hit else None

    # -- python ---------------------------------------------------------------------------

    def _python(self, path, text):
        package = _python_package_of(path)
        for match in _PY_IMPORT.finditer(text):
            for name in match.group(1).split(","):
                yield self._python_lookup(name.strip())
        for match in _PY_FROM.finditer(text):
            base = match.group(1)
            if base.startswith("."):
                depth = len(base) - len(base.lstrip("."))
                parent = package.split(".")[: max(len(package.split(".")) - depth + 1, 0)] if package else []
                base = ".".join([p for p in parent if p] + ([base.lstrip(".")] if base.lstrip(".") else []))
            names = [n.split()[0] for n in match.group(2).strip("()").split(",") if n.strip()]
            submodules = [self._python_lookup(f"{base}.{n}") for n in names if n != "*"]
            hits = {t for hit in submodules if hit for t in hit}
            yield hits or self._python_lookup(base)

    def _python_lookup(self, dotted):
        if not dotted:
            return None
        exact = self._python_index.get(dotted)
        if exact:
            return {exact}
        candidates = self._python_index.get("*" + dotted)
        if isinstance(candidates, str):
            return {candidates}
        # Ansible collections import themselves as ansible_collections.<namespace>.<name>.plugins...,
        # which is not the directory layout of the repository that holds the collection.
        parts = dotted.split(".")
        if parts[0] == "ansible_collections" and len(parts) > 4:
            return self._python_lookup(".".join(parts[3:]))
        return None

    # -- javascript / typescript ----------------------------------------------------------

    def _javascript(self, path, text):
        for spec in _JS_SPEC.findall(text):
            if spec.startswith("."):
                bases = [posixpath.normpath(posixpath.join(posixpath.dirname(path), spec))]
            else:
                bases = self._alias_bases(path, spec)
            yield next((hit for hit in (self._first_existing(_js_candidates(b)) for b in bases) if hit), None)

    def _alias_bases(self, path, spec):
        """Where an aliased import like `@/stores/x` may live, most likely first. Bare package
        imports get no candidates. A candidate only counts if the file exists."""
        project = self._js_project(posixpath.dirname(path))
        if project is not None:
            for prefix, bases in self._aliases(project):
                if spec == prefix.rstrip("/") or spec.startswith(prefix):
                    rest = spec[len(prefix):]
                    return [_join(base, rest) for base in bases]
        if spec.startswith(("@/", "~/")):  # the conventional alias for the project's source root
            rest = spec[2:]
            roots = [_join(project, "src"), project] if project is not None else []
            return [_join(root, rest) for root in roots] + [_join("src", rest)]
        return []

    def _js_project(self, directory):
        """The nearest ancestor directory holding a bundler config, tsconfig or package.json."""
        if directory in self._js_projects:
            return self._js_projects[directory]
        files = set(self._files_by_dir.get(directory, []))
        here = any(_join(directory, name) in files for name in _JS_BUNDLER_CONFIGS) or any(
            self._read_text(_join(directory, name)) is not None for name in _JS_PROJECT_MARKERS)
        if here:
            found = directory
        elif directory:
            found = self._js_project(posixpath.dirname(directory))
        else:
            found = None
        self._js_projects[directory] = found
        return found

    def _aliases(self, project):
        """Alias prefixes declared by the project's tsconfig/jsconfig `paths` and bundler config."""
        if project in self._js_aliases:
            return self._js_aliases[project]
        aliases = {}
        for name in _JS_TS_CONFIGS:
            options = (_loose_json(self._read_text(_join(project, name))) or {}).get("compilerOptions") or {}
            base_url = _join(project, options.get("baseUrl") or ".")
            for pattern, targets in (options.get("paths") or {}).items():
                if isinstance(targets, list) and pattern.endswith("*"):
                    prefix = pattern[:-1]
                    aliases.setdefault(prefix, [_join(base_url, t.rstrip("*")) for t in targets if isinstance(t, str)])
        for name in _JS_BUNDLER_CONFIGS:
            text = self._read_text(_join(project, name)) if _join(project, name) in self.paths else None
            for prefix, expression in _bundler_aliases(text or ""):
                literal = _PATH_LITERAL.search(expression)
                if literal:
                    bases = [_join(project, literal.group(1))]
                else:  # computed (`resolve(someDir)`): try the usual roots; only existing files count
                    bases = [_join(project, "src"), project]
                key = prefix if prefix.endswith("/") else prefix + "/"
                aliases.setdefault(key, bases)
        ordered = sorted(aliases.items(), key=lambda item: -len(item[0]))
        self._js_aliases[project] = ordered
        return ordered

    def _first_existing(self, candidates):
        for candidate in candidates:
            if candidate in self.paths:
                return {candidate}
        return None

    # -- go -------------------------------------------------------------------------------

    def _find_go_modules(self):
        # go.mod is not a source file, so probe every ancestor directory of the Go sources.
        directories = set()
        for path in self.paths:
            if path.endswith(".go"):
                directories.update(str(parent) for parent in PurePosixPath(path).parents)
        modules = {}
        for directory in directories:
            directory = "" if directory == "." else directory
            match = _GO_MODULE.search(self._read_text(posixpath.join(directory, "go.mod")) or "")
            if match:
                modules[match.group(1)] = directory
        return modules

    def _go(self, path, text):
        specs = _GO_SINGLE.findall(text)
        for block in _GO_BLOCK.findall(text):
            specs.extend(_GO_QUOTED.findall(block))
        for spec in specs:
            yield self._go_lookup(spec)

    def _go_lookup(self, spec):
        for module, module_dir in self._go_modules.items():
            if spec == module or spec.startswith(module + "/"):
                directory = posixpath.normpath(posixpath.join(module_dir, spec[len(module):].lstrip("/")))
                directory = "" if directory == "." else directory
                files = {f for f in self._files_by_dir.get(directory, []) if f.endswith(".go") and not f.endswith("_test.go")}
                return files or None
        return None

    # -- rust -----------------------------------------------------------------------------

    def _rust(self, path, text):
        crate_src = _rust_crate_src(path)
        here = posixpath.dirname(path)
        for anchor, rest in _RUST_USE.findall(text):
            segments = [s for s in rest.split("::") if s and s[0].islower()]
            base = {"crate": crate_src, "super": posixpath.dirname(here), "self": here}[anchor]
            yield self._rust_lookup(base, segments)
        for name in _RUST_MOD.findall(text):
            yield self._first_existing([posixpath.join(here, f"{name}.rs"), posixpath.join(here, name, "mod.rs")])

    def _rust_lookup(self, base, segments):
        for length in range(len(segments), 0, -1):
            stem = posixpath.join(base, *segments[:length]) if base else posixpath.join(*segments[:length])
            hit = self._first_existing([stem + ".rs", posixpath.join(stem, "mod.rs")])
            if hit:
                return hit
        return None

    # -- jvm ------------------------------------------------------------------------------

    def _jvm(self, path, text):
        for dotted in _JVM_IMPORT.findall(text):
            parts = dotted.split(".")
            hit = None
            for length in range(len(parts), 1, -1):
                hit = self._suffix_index.get("/".join(parts[:length]))
                if hit:
                    break
            yield {hit} if isinstance(hit, str) else None

    # -- c / c++ --------------------------------------------------------------------------

    def _c(self, path, text):
        for include in _C_INCLUDE.findall(text):
            local = posixpath.normpath(posixpath.join(posixpath.dirname(path), include))
            unique = self._suffix_index.get(PurePosixPath(include).with_suffix("").as_posix())
            yield self._first_existing([local, include]) or ({unique} if isinstance(unique, str) else None)

    # -- ruby / php -----------------------------------------------------------------------

    def _ruby(self, path, text):
        for spec in _RUBY_REQUIRE.findall(text):
            base = posixpath.normpath(posixpath.join(posixpath.dirname(path), spec))
            yield self._first_existing([base, base + ".rb"])

    def _php(self, path, text):
        for spec in _PHP_REQUIRE.findall(text):
            base = posixpath.normpath(posixpath.join(posixpath.dirname(path), spec.lstrip("/")))
            yield self._first_existing([base])


def _python_package_of(path):
    parts = PurePosixPath(path).with_suffix("").parts
    if parts and parts[-1] == "__init__":
        return ".".join(parts[:-1])
    return ".".join(parts[:-1])


def _python_module_index(paths):
    """Map dotted names to files. Keys prefixed with '*' are unique trailing-suffix matches,
    which handles src-layouts and monorepos without knowing the sys.path."""
    exact, suffixes = {}, defaultdict(set)
    for path in paths:
        if not path.endswith((".py", ".pyi")):
            continue
        parts = list(PurePosixPath(path).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        if not parts:
            continue
        exact[".".join(parts)] = path
        for start in range(1, len(parts)):
            suffixes[".".join(parts[start:])].add(path)
    index = dict(exact)
    for dotted, hits in suffixes.items():
        if len(hits) == 1:
            index["*" + dotted] = next(iter(hits))
    return index


def _suffix_index(paths):
    """Map extension-less path suffixes to a file when exactly one file has that suffix."""
    seen = defaultdict(set)
    for path in paths:
        parts = PurePosixPath(path).with_suffix("").parts
        for start in range(len(parts)):
            seen["/".join(parts[start:])].add(path)
    return {suffix: next(iter(hits)) for suffix, hits in seen.items() if len(hits) == 1}


def _join(*parts):
    """Normalized repo-relative path; the repository root is ''."""
    joined = posixpath.normpath(posixpath.join(*(part or "" for part in parts)))
    return "" if joined == "." else joined


def _loose_json(text):
    """Parse JSON that may carry comments and trailing commas (tsconfig style). None on failure."""
    if not text:
        return None
    stripped = _JSON_COMMENT.sub(lambda m: m.group(0) if m.group(0).startswith('"') else "", text)
    try:
        value = json.loads(_JSON_TRAILING_COMMA.sub(r"\1", stripped))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _bundler_aliases(text):
    """(prefix, expression) pairs from `alias: {...}` / `alias: [{find, replacement}]` blocks."""
    for block in _ALIAS_BLOCK.finditer(text):
        body = text[block.end():block.end() + 2000]
        depth = 1
        for index, char in enumerate(body):
            depth += char in "{[("
            depth -= char in "}])"
            if depth == 0:
                body = body[:index]
                break
        yield from _ALIAS_FIND.findall(body)
        yield from _ALIAS_ENTRY.findall(body)


def _js_candidates(base):
    stem, ext = posixpath.splitext(base)
    candidates = [base]
    if ext in (".js", ".jsx", ".mjs", ".cjs"):
        candidates += [stem + e for e in (".ts", ".tsx", ".mts", ".cts")]
    candidates += [base + e for e in _JS_EXTENSIONS]
    candidates += [posixpath.join(base, "index" + e) for e in _JS_EXTENSIONS]
    return candidates


def _rust_crate_src(path):
    parts = PurePosixPath(path).parts
    if "src" in parts:
        return "/".join(parts[: len(parts) - 1 - parts[::-1].index("src") + 1])
    return ""

"""Per-language knowledge needed for cheap static signals: extensions, comments, function starts."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Language:
    name: str
    line_comments: tuple
    block_comment: tuple = ()  # (open, close)
    function_start: re.Pattern = None
    indent_scoped: bool = False  # bodies delimited by indentation rather than braces/`end`


def _re(pattern):
    return re.compile(pattern, re.MULTILINE)


_C_STYLE = ("//",)
_C_BLOCK = ("/*", "*/")
_JAVA_LIKE_FN = _re(
    r"^\s*(?:(?:public|private|protected|internal|static|final|override|virtual|abstract|"
    r"async|synchronized|open|suspend)\s+)+[\w<>\[\],.?\s]*?\b\w+\s*\([^;{}]*\)\s*(?:throws\s+[\w.,\s]+)?\{?\s*$"
)
_JS_FN = _re(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\b"
    r"|^\s*(?:export\s+)?(?:const|let|var)\s+\w+\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>"
    r"|^\s*(?:(?:public|private|protected|static|async|get|set)\s+)*(?!(?:if|for|while|switch|catch|return|function)\b)\w+\s*\([^)]*\)\s*(?::\s*[^={]+)?\{\s*$"
)

# C and C++ definitions: a return type and a name on one line, possibly indented (functions inside
# a namespace, inline methods). Control keywords are excluded; a lone call like `MACRO(x)` has no
# return type and does not match; a prototype ends in `;` and does not match either.
_C_FAMILY_KEYWORDS = r"(?!(?:if|else|for|while|switch|catch|return|do|case|throw|delete|new|goto|sizeof|co_return|co_await)\b)"
_C_FN = _re(r"^\s*" + _C_FAMILY_KEYWORDS + r"[A-Za-z_][\w\s\*]*?\b\w+\s*\([^;]*\)\s*\{?\s*$")
_CPP_FN = _re(
    r"^\s*" + _C_FAMILY_KEYWORDS + r"[A-Za-z_][\w\s\*&:<>,~]*?\b[\w:~]+\s*\([^;]*\)\s*"
    r"(?:const\s*)?(?:noexcept\s*)?(?:override\s*)?(?:final\s*)?(?:->\s*[\w:<>,\s\*&]+?)?\{?\s*$"
)

_LANGUAGES = {
    "python": Language("python", ("#",), ('"""', '"""'), _re(r"^\s*(?:async\s+)?def\s+\w+"), indent_scoped=True),
    "javascript": Language("javascript", _C_STYLE, _C_BLOCK, _JS_FN),
    "typescript": Language("typescript", _C_STYLE, _C_BLOCK, _JS_FN),
    "go": Language("go", _C_STYLE, _C_BLOCK, _re(r"^func\s")),
    "rust": Language("rust", _C_STYLE, _C_BLOCK, _re(r"^\s*(?:pub(?:\([\w:]+\))?\s+)?(?:async\s+)?(?:const\s+)?(?:unsafe\s+)?fn\s+\w+")),
    "java": Language("java", _C_STYLE, _C_BLOCK, _JAVA_LIKE_FN),
    "csharp": Language("csharp", _C_STYLE, _C_BLOCK, _JAVA_LIKE_FN),
    "kotlin": Language("kotlin", _C_STYLE, _C_BLOCK, _re(r"^\s*(?:[\w]+\s+)*fun\s+")),
    "scala": Language("scala", _C_STYLE, _C_BLOCK, _re(r"^\s*(?:[\w]+\s+)*def\s+\w+")),
    "swift": Language("swift", _C_STYLE, _C_BLOCK, _re(r"^\s*(?:[\w@]+\s+)*func\s+\w+")),
    "dart": Language("dart", _C_STYLE, _C_BLOCK, _JAVA_LIKE_FN),
    "c": Language("c", _C_STYLE, _C_BLOCK, _C_FN),
    "cpp": Language("cpp", _C_STYLE, _C_BLOCK, _CPP_FN),
    "php": Language("php", ("//", "#"), _C_BLOCK, _re(r"^\s*(?:(?:public|private|protected|static|final|abstract)\s+)*function\s+\w+")),
    "ruby": Language("ruby", ("#",), ("=begin", "=end"), _re(r"^\s*def\s+")),
    "lua": Language("lua", ("--",), ("--[[", "]]"), _re(r"^\s*(?:local\s+)?function\b")),
    "elixir": Language("elixir", ("#",), (), _re(r"^\s*defp?\s+\w+")),
    "shell": Language("shell", ("#",), (), _re(r"^\s*(?:function\s+\w+|\w+\s*\(\)\s*\{)")),
    "vue": Language("vue", _C_STYLE, _C_BLOCK, _JS_FN),
    "svelte": Language("svelte", _C_STYLE, _C_BLOCK, _JS_FN),
    "sql": Language("sql", ("--",), _C_BLOCK, _re(r"^\s*create\s+(?:or\s+replace\s+)?(?:function|procedure)\b")),
    "r": Language("r", ("#",), (), _re(r"\w+\s*<-\s*function\s*\(")),
    "julia": Language("julia", ("#",), ("#=", "=#"), _re(r"^\s*function\s+\w+")),
    "zig": Language("zig", ("//",), (), _re(r"^\s*(?:pub\s+)?fn\s+\w+")),
    "haskell": Language("haskell", ("--",), ("{-", "-}"), None, indent_scoped=True),
    "ocaml": Language("ocaml", (), ("(*", "*)"), _re(r"^\s*let\s+(?:rec\s+)?\w+.*=")),
    "clojure": Language("clojure", (";",), (), _re(r"^\s*\(defn-?\s")),
    "perl": Language("perl", ("#",), (), _re(r"^\s*sub\s+\w+")),
    "groovy": Language("groovy", _C_STYLE, _C_BLOCK, _re(r"^\s*(?:[\w<>]+\s+)*def\s+\w+|" + _JAVA_LIKE_FN.pattern)),
}

_EXTENSIONS = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".go": "go", ".rs": "rust", ".java": "java", ".cs": "csharp",
    ".kt": "kotlin", ".kts": "kotlin", ".scala": "scala", ".swift": "swift", ".dart": "dart",
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
    ".m": "c", ".mm": "cpp",
    ".php": "php", ".rb": "ruby", ".lua": "lua", ".ex": "elixir", ".exs": "elixir",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".vue": "vue", ".svelte": "svelte", ".sql": "sql", ".r": "r", ".jl": "julia", ".zig": "zig",
    ".hs": "haskell", ".ml": "ocaml", ".clj": "clojure", ".cljs": "clojure",
    ".pl": "perl", ".pm": "perl", ".groovy": "groovy", ".gradle": "groovy",
}

BRANCH_PATTERN = re.compile(
    r"\b(?:if|elif|elsif|else\s+if|for|foreach|while|until|unless|case|when|catch|except|rescue|guard)\b|&&|\|\|"
)


def language_for(path, extra_extensions=None):
    """Return the Language for a path, or None when the file is not source code we analyze."""
    suffix = path.suffix.lower()
    name = (extra_extensions or {}).get(suffix) or _EXTENSIONS.get(suffix)
    return _LANGUAGES.get(name) if name else None


def known_languages():
    return sorted(_LANGUAGES)

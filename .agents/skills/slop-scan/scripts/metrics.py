"""Cheap, language-agnostic size, complexity and slop-fingerprint signals.

These are hints that steer the workers toward hotspots and feed the report's fingerprint
table; they are approximate by design and are never reported as findings on their own. The
fingerprint counters count habits that careless or generated code leaves behind: broad
catches that swallow errors, logging every step, hedging fallbacks, and comments that
restate the next line.
"""

import re
from collections import Counter

from languages import BRANCH_PATTERN

LONG_FUNCTION_LINES = 100
_TODO = re.compile(r"\b(?:TODO|FIXME|HACK|XXX)\b")
_STRING_LITERAL = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`')
END_KEYWORD_LANGUAGES = {"ruby", "lua", "elixir", "julia"}

_LOG_CALL = re.compile(
    r"\bBOOST_LOG(?:_TRIVIAL|_SEV)?\s*\(|\b(?:D|V)?LOG(?:_\w+)?\s*\(|\bLOG[DIWEV]\s*\(|\bspdlog::\w+\s*\(|\bqDebug\s*\(|\bNSLog\s*\("
    r"|\bconsole\.(?:log|debug|info|warn|error|trace)\s*\("
    r"|\b(?:logger|logging|log|_log|_logger|LOGGER|Log|Logger)\s*\.\s*(?:trace|debug|info|warn|warning|error|exception|critical|fatal|verbose|print\w*)\s*\("
    r"|\.Log(?:Trace|Debug|Information|Warning|Error|Critical)\s*\("
    r"|\b(?:trace|debug|info|warn|error)!\s*\("
)
_CATCH_ALL = re.compile(
    r"\bcatch\s*\(\s*\.\.\.\s*\)"  # C++ catch (...)
    r"|\bcatch\s*\(\s*(?:const\s+)?(?:std::)?exception\b"  # C++ std::exception
    r"|\bcatch\s*\(\s*(?:final\s+)?(?:System\.)?(?:Exception|Throwable)\b"  # Java, C#
    r"|\bcatch\s*(?:\(\s*\w*\s*(?::\s*\w+\s*)?\))?\s*\{"  # JS/TS/C#: catch (e) { / catch {
    r"|^\s*except\s*(?:\(?\s*(?:Exception|BaseException)\b[^:]*)?:"  # Python bare or broad except
    r"|^\s*rescue\s*(?:=>\s*\w+)?\s*$"  # Ruby bare rescue
    r"|\.catch\s*\(\s*(?:\(\s*\w*\s*\)|\w+)\s*=>",  # promise.catch(() => ...)
    re.MULTILINE,
)
# A statement that does nothing with an error: a default return, a literal assignment, a loop skip.
_INERT_STATEMENT = re.compile(
    r"^(?:return(?:\s+(?:[\w:.]+|-?\d+|\"[^\"]*\"|'[^']*'|\{\s*\}|\[\s*\]|\(\s*\)))?\s*;?"
    r"|pass|continue\s*;?|break\s*;?|;"
    r"|[\w.\[\]\"'>-]+\s*=\s*(?:true|false|True|False|nullptr|null|None|undefined|-?\d+|\"\"|''|\{\s*\}|\[\s*\]|std::nullopt)\s*;?)$"
)
_HEDGE = re.compile(r"fall[\s_-]?back|best[\s_-]?effort|work[\s_-]?around|just[\s_-]in[\s_-]case|safety[\s_-]net", re.IGNORECASE)
_WORD = re.compile(r"[A-Za-z]+")
_IDENTIFIER_PART = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")
_STOPWORDS = {
    "the", "a", "an", "if", "is", "are", "to", "of", "for", "and", "or", "we", "this", "it", "in", "on", "with",
    "from", "into", "by", "be", "now", "then", "new", "all", "any", "its", "our", "that", "as", "at", "up", "out",
}
# Verbs a narrating comment uses to restate the next line ("Create the socket" above `create_socket()`).
_NARRATING_VERBS = {
    "set", "get", "call", "create", "check", "return", "initialize", "init", "update", "increment", "decrement",
    "add", "remove", "loop", "iterate", "compute", "calculate", "store", "save", "load", "read", "write", "open",
    "close", "start", "stop", "log", "print", "handle", "process", "convert", "parse", "build", "make", "send",
    "receive", "wait", "clear", "reset", "append", "insert", "delete", "assign", "declare", "define", "validate",
    "ensure", "fetch", "apply", "emit", "register", "copy", "move", "use", "run", "invoke", "construct", "free",
    "release", "allocate", "lock", "unlock", "notify", "push", "pop", "find", "lookup", "mark", "enable", "disable",
}


def measure(text, language):
    lines = text.splitlines()
    kinds = classify_lines(lines, language)
    code_lines = [line for line, kind in zip(lines, kinds) if kind == "code"]
    function_lengths = _function_lengths(lines, kinds, language)
    catches, swallowed = _catches(lines, kinds, language)
    return {
        "lines": len(lines),
        "loc": len(code_lines),
        "comment_lines": kinds.count("comment"),
        "max_nesting": _max_nesting(code_lines),
        "branch_points": sum(len(BRANCH_PATTERN.findall(line)) for line in code_lines),
        "functions": len(function_lengths),
        "longest_function": max(function_lengths, default=0),
        "long_functions": sum(1 for length in function_lengths if length > LONG_FUNCTION_LINES),
        "todo_markers": len(_TODO.findall(text)),
        "log_calls": sum(len(_LOG_CALL.findall(line)) for line in code_lines),
        "catch_all": catches,
        "swallowed_catches": swallowed,
        "hedges": sum(len(_HEDGE.findall(line)) for line, kind in zip(lines, kinds) if kind != "blank"),
        "narrating_comments": _narrating_comments(lines, kinds, language),
    }


def _catches(lines, kinds, language):
    """Broad catches, and how many swallow the error: the body is empty, or only logs, returns a
    default value, or assigns a literal. Converting the error into something else (an HTTP error
    response, a result object) is handling, not swallowing."""
    total = swallowed = 0
    for index, line in enumerate(lines):
        if kinds[index] != "code" or not _CATCH_ALL.search(line):
            continue
        total += 1
        body = _indented_body(lines, kinds, index) if language.indent_scoped else _braced_body(lines, kinds, index)
        if body is not None and all(_inert(statement) for statement in body):
            swallowed += 1
    return total, swallowed


def _inert(statement):
    return bool(_LOG_CALL.search(statement) or _INERT_STATEMENT.match(statement))


def _braced_body(lines, kinds, start, limit=12):
    """Statements between the catch's braces (comments left out), or None when unclear."""
    depth, opened, body = 0, False, []
    for index in range(start, min(start + limit, len(lines))):
        code = _STRING_LITERAL.sub('""', lines[index]).split("//", 1)[0]
        if index == start:
            code = code[code.find("catch"):] if "catch" in code else code
        if kinds[index] != "code":
            continue
        current = []
        for char in code:
            if char == "{":
                depth += 1
                if not opened:
                    opened = True
                    continue
            elif char == "}":
                depth -= 1
            if opened and depth <= 0:
                statement = "".join(current).strip()
                return body + ([statement] if statement else [])
            if opened and depth >= 1:
                current.append(char)
        statement = "".join(current).strip()
        if statement:
            body.append(statement)
    return None


def _indented_body(lines, kinds, start, limit=12):
    indent = _indent_width(lines[start])
    body = []
    for index in range(start + 1, min(start + limit, len(lines))):
        if kinds[index] == "blank":
            continue
        if _indent_width(lines[index]) <= indent:
            return body
        if kinds[index] == "code":
            body.append(lines[index].strip())
    return body if body else None


def _narrating_comments(lines, kinds, language):
    """Whole-line comments that only restate the code line right below them."""
    count = 0
    for index, kind in enumerate(kinds):
        if kind != "comment" or not language.line_comments:
            continue
        stripped = lines[index].strip()
        prefix = next((p for p in language.line_comments if stripped.startswith(p)), None)
        following = next((i for i in range(index + 1, min(index + 3, len(lines))) if kinds[i] != "blank"), None)
        if prefix is None or following is None or kinds[following] != "code":
            continue
        words = [w.lower() for w in _WORD.findall(stripped[len(prefix):])]
        content = [w for w in words if w not in _STOPWORDS]
        if not 2 <= len(content) <= 8:
            continue
        code_words = {part.lower() for token in _WORD.findall(lines[following]) for part in _IDENTIFIER_PART.findall(token)}
        code_words |= {token.lower() for token in _WORD.findall(lines[following])}
        stems = {w.rstrip("s") for w in code_words}
        mentioned = [w for w in content if w in code_words or w.rstrip("s") in stems]
        if mentioned and all(w in code_words or w.rstrip("s") in stems or w in _NARRATING_VERBS for w in content):
            count += 1
    return count


def classify_lines(lines, language):
    """Label each line as 'blank', 'comment' or 'code'."""
    kinds = []
    block_close = None
    for line in lines:
        stripped = line.strip()
        if block_close:
            kinds.append("comment")
            if block_close in stripped:
                block_close = None
            continue
        if not stripped:
            kinds.append("blank")
            continue
        if language.block_comment and stripped.startswith(language.block_comment[0]):
            opener, closer = language.block_comment
            if closer not in stripped[len(opener):]:
                block_close = closer
            kinds.append("comment")
            continue
        if any(stripped.startswith(prefix) for prefix in language.line_comments):
            kinds.append("comment")
            continue
        kinds.append("code")
    return kinds


def _indent_width(line):
    expanded = line.expandtabs(4)
    return len(expanded) - len(expanded.lstrip())


def _max_nesting(code_lines):
    widths = [_indent_width(line) for line in code_lines]
    positive = [w for w in widths if w > 0]
    if not positive:
        return 0
    # The most common small indent is the file's indent unit (2, 4, a tab...).
    unit = Counter(w for w in positive if w <= 8).most_common(1)
    unit = max(unit[0][0] if unit else min(positive), 2)
    return max(widths) // unit


def _function_lengths(lines, kinds, language):
    if language.function_start is None:
        return []
    by_indent = language.indent_scoped or language.name in END_KEYWORD_LANGUAGES
    lengths = []
    for index, line in enumerate(lines):
        if kinds[index] != "code" or not language.function_start.search(line):
            continue
        end = _indent_block_end(lines, kinds, index) if by_indent else brace_block_end(lines, index)
        if end is not None:
            lengths.append(end - index + 1)
    return lengths


def _indent_block_end(lines, kinds, start):
    start_indent = _indent_width(lines[start])
    last_body_line = start
    for index in range(start + 1, len(lines)):
        if kinds[index] == "blank":
            continue
        if _indent_width(lines[index]) <= start_indent:
            # A closing `end` belongs to the function; anything else starts the next block.
            return index if lines[index].strip() == "end" else last_body_line
        last_body_line = index
    return last_body_line


def brace_block_end(lines, start, lookahead=3):
    depth = 0
    opened = False
    for index in range(start, len(lines)):
        code = _STRING_LITERAL.sub('""', lines[index]).split("//", 1)[0]
        depth += code.count("{") - code.count("}")
        opened = opened or "{" in code
        if not opened and index - start >= lookahead:
            return None  # a declaration or prototype, not a definition
        if opened and depth <= 0:
            return index
    return None

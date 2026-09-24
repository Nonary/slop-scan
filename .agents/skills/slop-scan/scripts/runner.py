"""Run a tier's tasks as a pool of `codex exec` workers.

Each task is one fresh, ephemeral, read-only Codex session in the scanned repository. The
prompt carries everything the worker needs (role, rubric, contract, task, and usually the
code itself), and the worker answers with a single JSON object in its final message. The
runner extracts it, drops malformed items, and asks for one repair if the shape is wrong.
Separate processes each get their own session budget, so the pool size is the only limit.
"""

import json
import shutil
import subprocess
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import progress as progresslib
import prompts
from inventory import load_manifest
from schema import sanitize, validate_output
from workspace import read_json, write_json


class WorkerSettings:
    def __init__(self, model, effort, timeout, codex="codex"):
        self.model = model
        self.effort = effort
        self.timeout = timeout
        self.codex = shutil.which(codex) or codex


def run_tier(workspace, tier, parallel, settings, echo=print):
    """Run every pending task of a tier with `parallel` concurrent workers. Returns the progress state."""
    state = progresslib.progress(workspace, tier)
    pending = state["pending"]
    if not pending:
        return state
    manifest = load_manifest(workspace)
    lock = threading.Lock()
    root = workspace.root()
    echo(f"{tier}: {len(pending)} tasks, {parallel} at a time ({settings.model}, {settings.effort} effort)")

    def work(task_id):
        task = read_json(workspace.tasks_dir(tier) / f"{task_id}.json")
        while True:
            with lock:
                attempt = progresslib.record_attempt(workspace, task_id)
            started = time.time()
            outcome = _attempt(workspace, tier, task, manifest, root, settings, lock)
            if outcome["ok"] or attempt >= progresslib.MAX_ATTEMPTS:
                return {**outcome, "attempt": attempt, "seconds": round(time.time() - started)}

    finished = 0
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = {pool.submit(work, task_id): task_id for task_id in pending}
        for future in as_completed(futures):
            finished += 1
            task_id = futures[future]
            try:
                outcome = future.result()
            except Exception as exc:  # a crashed worker thread must not stop the pool
                outcome = {"ok": False, "detail": f"runner error: {exc}", "attempt": "?", "seconds": 0}
            status = "ok" if outcome["ok"] else "FAILED"
            echo(f"  [{finished}/{len(pending)}] {task_id} {status} {outcome['detail']} "
                 f"(attempt {outcome['attempt']}, {outcome['seconds']}s)")
    return progresslib.progress(workspace, tier)


def _attempt(workspace, tier, task, manifest, root, settings, lock):
    task_id = task["task_id"]
    prompt = prompts.build(tier, task, root, workspace.run()["history"])
    answer = _exec(workspace, task_id, "run", prompt, root, settings, lock)
    if answer is None:
        return {"ok": False, "detail": "codex exec failed or timed out"}

    output, problems, dropped = _check(tier, answer, task, manifest)
    if problems:
        repair = prompts.repair(prompt, answer, problems)
        answer = _exec(workspace, task_id, "repair", repair, root, settings, lock)
        if answer is None:
            return {"ok": False, "detail": "repair call failed"}
        output, problems, dropped = _check(tier, answer, task, manifest)
        if problems:
            return {"ok": False, "detail": "; ".join(problems[:3])}

    write_json(workspace.output_for(task_id, tier), output)
    counts = [f"{len(output[key])} {key}" for key in ("findings", "decisions", "priorities") if isinstance(output.get(key), list)]
    if dropped:
        counts.append(f"{len(dropped)} malformed items dropped")
    return {"ok": True, "detail": ", ".join(counts)}


def _check(tier, answer, task, manifest):
    """Return (sanitized output, structural problems, dropped item messages)."""
    parsed = _extract_json(answer)
    if parsed is None:
        return None, ["the reply did not contain a JSON object"], []
    output, dropped = sanitize(tier, parsed, task, manifest)
    return output, validate_output(tier, output, task, manifest), dropped


def _extract_json(text):
    """Accept a bare object, a fenced block, or an object embedded in prose."""
    text = text.strip()
    for candidate in (text, text[text.find("{"):text.rfind("}") + 1] if "{" in text else ""):
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            continue
    return None


def _exec(workspace, task_id, label, prompt, root, settings, lock):
    """Run one ephemeral codex session; return its final message, or None on failure."""
    logs = workspace.dir / "logs"
    logs.mkdir(exist_ok=True)
    last_message = logs / f"{task_id}.{label}.last.txt"
    events = logs / f"{task_id}.{label}.jsonl"
    command = [
        settings.codex, "exec", "--json", "--ephemeral", "--skip-git-repo-check",
        "-s", "read-only", "-m", settings.model,
        "-c", f'model_reasoning_effort="{settings.effort}"',
        *_disabled_mcp_servers(),
        "-C", str(root), "-o", str(last_message), "-",
    ]
    try:
        with open(events, "w") as out:
            result = subprocess.run(command, input=prompt, text=True, stdout=out,
                                    stderr=subprocess.STDOUT, timeout=settings.timeout)
    except subprocess.TimeoutExpired:
        return None
    with lock:
        _record_usage(workspace, events)
    if result.returncode != 0 or not last_message.exists():
        return None
    return last_message.read_text(encoding="utf-8", errors="replace")


_MCP_FLAGS = None


def _disabled_mcp_servers():
    """Workers only need the shell. Starting the user's MCP servers in every session is slow."""
    global _MCP_FLAGS
    if _MCP_FLAGS is None:
        try:
            with open(Path.home() / ".codex" / "config.toml", "rb") as fh:
                servers = tomllib.load(fh).get("mcp_servers", {})
        except (OSError, tomllib.TOMLDecodeError):
            servers = {}
        _MCP_FLAGS = [flag for name in servers for flag in ("-c", f"mcp_servers.{name}.enabled=false")]
    return _MCP_FLAGS


def _record_usage(workspace, events):
    path = workspace.dir / "usage.json"
    usage = read_json(path) if path.exists() else {"sessions": 0, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    usage["sessions"] += 1
    for line in events.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "turn.completed":
            for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
                usage[key] += event.get("usage", {}).get(key, 0)
    write_json(path, usage)

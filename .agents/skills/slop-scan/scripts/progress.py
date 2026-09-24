"""Which tasks of a tier are done, failed, or still pending.

The runner only writes an output after it has been sanitized and validated, so an existing
output file means the task is done. Attempts are counted so a task that keeps failing is
eventually given up on instead of blocking the scan.
"""

from workspace import read_json, write_json

MAX_ATTEMPTS = 3


def progress(workspace, tier):
    """Return None if the tier is not planned, else {"done", "failed", "pending", "tasks"}."""
    if not workspace.tasks_dir(tier).exists():
        return None
    attempts = _attempts(workspace)
    state = {"done": [], "failed": [], "pending": []}
    for task_file in workspace.task_files(tier):
        task_id = task_file.stem
        if workspace.output_for(task_id, tier).exists():
            state["done"].append(task_id)
        elif attempts.get(task_id, 0) >= MAX_ATTEMPTS:
            state["failed"].append(task_id)
        else:
            state["pending"].append(task_id)
    state["tasks"] = sum(len(ids) for ids in state.values())
    return state


def record_attempt(workspace, task_id):
    """Count one attempt at a task. Only the runner's coordinating thread calls this."""
    attempts = _attempts(workspace)
    attempts[task_id] = attempts.get(task_id, 0) + 1
    write_json(workspace.dir / "attempts.json", attempts)
    return attempts[task_id]


def _attempts(workspace):
    path = workspace.dir / "attempts.json"
    return read_json(path) if path.exists() else {}

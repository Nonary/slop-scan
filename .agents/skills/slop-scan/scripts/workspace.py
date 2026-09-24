"""Layout of a slop-scan run directory and small JSON helpers shared by every command."""

import json
from pathlib import Path

from lenses import lens_payload

SKILL_DIR = Path(__file__).resolve().parent.parent

TIERS = ("tier1", "tier2", "tier3", "flows", "editor", "appeal", "chief")


class Workspace:
    """All state for one scan lives under a single run directory, so any step can resume."""

    def __init__(self, workdir):
        self.dir = Path(workdir).resolve()

    @property
    def run_file(self):
        return self.dir / "run.json"

    @property
    def manifest_file(self):
        return self.dir / "manifest.json"

    @property
    def graph_file(self):
        return self.dir / "graph.json"

    @property
    def clones_file(self):
        return self.dir / "clones.json"

    def clones(self):
        """Duplication evidence from tier 0; empty for runs made before clone detection existed."""
        path = self.clones_file
        return read_json(path) if path.exists() else {"pairs": [], "total_pairs": 0, "module_pairs": [], "parallel_trees": []}

    @property
    def reach_file(self):
        return self.dir / "reach.json"

    def reach(self):
        """What checked-in configuration runs, from tier 0; None for runs made before it existed."""
        return read_json(self.reach_file) if self.reach_file.exists() else None

    @property
    def findings_file(self):
        return self.dir / "findings.json"

    @property
    def reports_dir(self):
        return self.dir / "reports"

    def tasks_dir(self, tier):
        return self.dir / "tasks" / tier

    def out_dir(self, tier):
        return self.dir / "out" / tier

    def task_files(self, tier):
        return sorted(self.tasks_dir(tier).glob("*.json"))

    def output_for(self, task_id, tier):
        return self.out_dir(tier) / f"{task_id}.json"

    def run(self):
        return read_json(self.run_file)

    def root(self):
        return Path(self.run()["root"])


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def task_header(tier, task_id, root, lens=None):
    """Fields every worker task starts with: who it is, where the code is, what to look for."""
    return {
        "task_id": task_id,
        "tier": tier,
        "root": str(root),
        **({"lens": lens_payload(lens)} if lens else {}),
    }


def task_id_for(prefix, number, lens):
    """T1-007 for a combined pass, T1-007-hygiene when the work is split by lens."""
    base = f"{prefix}-{number:03d}"
    return base if lens.name == "all" else f"{base}-{lens.name}"

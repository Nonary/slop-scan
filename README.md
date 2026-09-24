# slop-scan

A Codex skill that runs a pragmatic, multi-tier **maintainability** review of an entire codebase.
It looks for poor structure and design (cohesion, coupling, complexity, duplication, abstraction,
layering, dead weight, and the tell-tale "slop" of careless or generated code), not bugs. Then
it prunes its own findings the way a pragmatic senior engineer would.

```
tier 0  inventory   script              metrics and slop counters, import graph and cycles, git churn and file age,
                                        definition clusters, copied code and parallel trees, reachability, shards
tier 1  scouts      ~1 per 1,200 LOC    file cards + file-local findings
tier 2  architects  ~1 per module group cross-file findings + module summaries
tier 3  systems     1                   architecture summary, themes, cross-module findings, flows to trace
flows   tracers     1 per flow (2-6)    each critical workflow traced end to end across files
editor  editors     ~1 per 10 findings  verify, merge, re-rate (up or down), or drop each finding
appeal  appeals     ~1 per 10 drops     second opinion on medium+ findings the editors dropped or downgraded
chief   chief       1                   executive summary, final themes, fix-first list
report  script                          SLOP_REPORT.md + slop_report.html, graded A–F
```

## How it runs

`scripts/slop.py scan` runs a fixed pool of `codex exec` workers (`--parallel`, default 6),
with no subagents:

- **One short, focused task per session.** Each worker is a fresh `--ephemeral`, **read-only**
  session in the scanned repo, on `gpt-6-luna` at medium effort. The prompt is self-contained:
  role, rubric, output contract, task, and for scouts and editors the code itself with line
  numbers, so workers rarely need tools. A scout task takes about 25s and 35k input tokens.
- **Forgiving about details, strict about shape.** The worker's final message is the JSON answer.
  The runner extracts it, drops individual malformed findings, makes one repair call if the
  structure is wrong, and retries a task up to 3 times before skipping it (the report flags the gap).
- **Separate processes, separate budgets.** Each `codex exec` is its own session, so there's
  no shared subagent cap. `--parallel` and your provider are the only limits.
- **Resumable.** All state lives in `.slop_scan/<project>/`, and re-running `scan` continues where it stopped.
- **Quiet.** Workers get your MCP servers disabled (they only need the shell), and token usage is recorded in `usage.json`.

A 110k-LOC C++ repo is about 145 scouts, about 20 architects, 1 systems pass, a few flow tracers,
editors, appeals, and 1 chief: roughly 220 short sessions.

## Deterministic evidence

Some things a script can measure exactly, and workers cannot prune them:

- **Copied code.** Token-level clone detection (winnowed fingerprints, comments and literal values
  ignored) finds blocks pasted between files anywhere in the repository. Each file's copies are
  handed to its scout, each module's to its architect, and the largest to the systems pass.
- **Parallel trees.** Sibling directories that share many file names or much copied code (a
  second front end, a v1 and v2 of a subsystem), with how often the two change in the same commit.
- **Slop fingerprint.** Per-module rates of swallowed catches, broad catches, logging calls,
  fallback/best-effort mentions, comments that restate the next line, and functions over 100
  lines. Modules at twice the repository rate are flagged.
- **Reachability.** What the checked-in configuration actually runs: containers, compose, CI,
  Procfiles, Makefiles, package.json, pyproject and setup files, shell scripts, and the files a
  framework runs by convention (Next, Nuxt, SvelteKit, Remix, Astro, TanStack and Nitro routes,
  tool configs, Django, Celery, Airflow and Ansible plugins). From those entry points the script
  follows imports, plus loads it can see: module names and source paths in string literals,
  packages that import their own directory, bundler globs, and generated files such as route
  trees. Every Python and JavaScript/TypeScript file gets a `reach`: `launched`, `manual` (only
  entry points nothing checked in starts reach it), `tests` or `none`. A migration left half done
  shows up here: a new subsystem only a hand-run worker or CLI reaches, beside the launched code
  that still does the same job. Docs never count as launching code. C/C++ and JVM/.NET files are
  not analyzed, because their imports don't say what links.
- **History.** The repository's real size and age, plus each file's commits in the last year and,
  for files created in that window, their age. Workers judge each file on its own numbers.
- **Dependencies.** C/C++ implementation files inherit their header's includers as `fan_in`, and
  JS/TS `@/`-style aliases are resolved from tsconfig `paths` and bundler configs.

## Pragmatism

The rubric (`references/taxonomy.md`) starts with a "Be pragmatic" charter that every worker
sees, and the editor, appeal and chief tiers enforce it:

- Describe the system that runs. When docstrings or docs disagree with what is launched, the
  disagreement is the finding. Two generations of one design (a rewrite left half done) are one
  `parallel-implementations` finding, and it comes before any refactor inside either generation.
- Things that change for the same reasons stay together, whatever SOLID says. Evidence, strongest first:
  1. **Reason to change:** can anyone name a plausible requirement that touches one part but not the other?
  2. **Git co-change:** files that change in the same commits. Withheld only when the whole repository
     is young (under 50 commits or 30 days), because scaffolding commits make everything look coupled.
  3. **Static structure:** each file's definitions are grouped into clusters that reference each other
     or share `self`/`this` state, and each cluster's callers are found. Separate clusters with separate
     callers mark a real seam; clusters with the same callers are used together.
- "It is all one workflow" does not excuse ownership or ordering a reader cannot see.
- A principle violation is not a finding by itself. Every finding must name a concrete maintenance cost.
- Rule of three for duplication, unless the copies must change in lockstep; size alone is not a defect;
  scripts and tests get a lower bar; habits (swallowed errors, narrating comments, log spam) are findings.
- Priority follows each file's traffic: recent commits and how much depends on it. New code in a central
  place is cheapest to fix now.
- Severity is the pain; effort is the fix. A large fix never lowers severity.

Dismissed findings stay visible (collapsed) in the report with the editor's reason. Medium or
higher findings that an editor dropped or downgraded get a second opinion from the appeal tier.

## Grade

The grade is the worst of three gates, and the report says which one set it: the slop index
(weighted findings per 1k LOC: A < 1, B < 2, C < 4, D < 8), serious findings regardless of size
(1+ critical caps at C, 5+ high at B, 10+ high at C, ...), and copied code (5% of non-test lines
caps at B, 10% at C, 20% at D). The bands are provisional. To check what a scan missed, list the
problems you expect and run `recall` (below).

## Layout

```
.agents/skills/slop-scan/
  SKILL.md                   how Codex drives a scan
  agents/openai.yaml         UI metadata; explicit invocation only ($slop-scan)
  references/taxonomy.md     rubric: pragmatism charter, categories, severity
  references/contracts.md    JSON answer shape per tier
  references/workers/*.md    role prompts: scout, architect, systems, flow, editor, appeal, chief
  scripts/slop.py            CLI (stdlib-only Python 3.11+)
  scripts/runner.py          codex exec worker pool
  scripts/prompts.py         self-contained prompt assembly
  scripts/inventory.py, metrics.py, imports.py, graph.py, gitlog.py   tier 0
  scripts/clones.py, fingerprint.py                                   copied code, parallel trees, slop fingerprint
  scripts/reach.py                                                    what checked-in configuration runs
  scripts/plan.py, findings.py, schema.py, report.py                  planning, merging, validation, output
  scripts/recall.py                                                   check a finished scan against known problems
  assets/report_template.html
```

## Use

In Codex, from this repo: `$slop-scan scan ~/sources/some-project`.

From a terminal:

```
S=.agents/skills/slop-scan/scripts/slop.py
python3 $S scan ~/sources/some-project --parallel 6    # everything; re-run to resume
python3 $S status --workdir .slop_scan/some-project
python3 $S recall --workdir .slop_scan/some-project --expect expected.json   # what did it miss?
```

`expected.json` lists problems you already know about: `[{"path": "src/stream.cpp", "lines": [2781, 2963],
"category": "coupling", "note": "shared runtime ownership inferred from counters"}]`. Only `path` is
required, and it may be a directory; `category` and `keywords` (any must appear in the finding's text)
keep a broad expectation from matching an unrelated finding. `recall` reports each as kept, found but
dismissed (with the reason), or never raised.

Options: `--no-tests`, `--exclude 'legacy/**'`, `--shard-loc N`, `--history-days N` (churn window,
default 365), `--deep` (3 narrow lenses per review, about 3× the sessions), `--model`, `--effort`,
`--timeout`, `--appeal-batch N`. Individual steps are also available: `inventory`, `plan <tier>`,
`run <tier>`, `report`, `recall`.

To use it in another repo, copy `.agents/skills/slop-scan/` there, or into `~/.agents/skills/` to make it global.

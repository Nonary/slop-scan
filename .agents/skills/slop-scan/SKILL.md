---
name: slop-scan
description: Pragmatic, multi-tier maintainability review of an entire codebase. A script measures what it can exactly (import graph, change history, copied code, a slop fingerprint), then fans the judgment out to a pool of focused `codex exec` workers (scouts per file batch, architects per module, a systems pass, flow tracers), and editor, appeal and chief passes decide what is worth fixing. Produces a graded SLOP_REPORT.md and an interactive HTML report. Use when the user asks for a slop scan, a code-quality or maintainability audit, or a design review of a whole repository or directory. Not for bug hunting or diff review.
---

# Slop Scan

A deterministic script runs the scan. Each unit of work is a short, focused, read-only
`codex exec` session on `gpt-6-luna` at medium effort, and a fixed pool runs several at once.
You launch it, keep the user posted, and present the result. You do not review code yourself,
spawn subagents, or read worker outputs.

```
tier 0  inventory  script   files, metrics, slop counters, import graph (C/C++ headers credit their .cpp),
                            git churn and file age, definition clusters, copied code, parallel trees,
                            reachability (what checked-in configuration launches), shards
tier 1  scouts     ~1 per 1,200 LOC      file cards + file-local findings (code inlined in the prompt)
tier 2  architects ~1 per module group   cross-file findings, module summaries
tier 3  systems    1                     architecture summary, themes, cross-module findings, flows to trace
flows   tracers    1 per flow (2-6)      trace startup/request/recovery/teardown across files
editor  editors    ~1 per 10 findings    verify, merge, re-rate up or down, drop hair-splitting
appeal  appeals    ~1 per 10 drops       second opinion on medium+ findings the editors dropped or downgraded
chief   chief      1                     executive summary, final themes, "fix first" list, last drops
report  script     SLOP_REPORT.md + slop_report.html
```

The judgment standard is `references/taxonomy.md`. Read its "Be pragmatic" section so you can
explain decisions: things that change for the same reasons stay together (judged by
reason-to-change, git co-change, and static clusters), "it is one workflow" does not excuse
hidden ownership or ordering, principles alone are not findings, the rule of three, size alone
is not a defect, habits are findings, priority follows each file's own traffic, and severity is
the pain while effort is the fix. `inventory` prints the repository's history (total commits,
age, the churn window), duplication and parallel trees, and reachability (lines launched, reached
only from entry points nothing starts, only by tests, by nothing); pass those on in one line.

`SLOP` below means `python3 <this skill's directory>/scripts/slop.py`.

## 1. Size it

```
SLOP inventory <target>
```

This takes seconds and prints files, LOC, the number of scout tasks, the history, the share of
copied code and any parallel trees. Tell the user the scale in one line. Rough total: scout
tasks × 1.5 sessions, about 35k input tokens each, at 20-60s per session divided across the
workers. Only ask before starting when there are more than 400 scout tasks. In that case offer
`--exclude` for vendored or generated trees, or `--no-tests`. Churn is counted over the last
365 days of history; `--history-days` changes that.
To change inventory options, delete the run directory and run `inventory` again.

## 2. Run it

`scan` spawns `codex exec` processes that need network access to the model, so run it **outside
the sandbox** (request escalated permissions). It runs for minutes to an hour, so start it in the
background and log to a file:

```
nohup SLOP scan <target> --parallel 6 > .slop_scan/<name>.log 2>&1 &
```

- `--parallel` is the number of concurrent workers. 6 is a safe default. Go higher only if the user's
  provider allows it.
- Every tier is resumable. If the process dies, run the same `scan` command again and it continues
  where it stopped (pass `--workdir .slop_scan/<name>`, and omit `<target>` if you like).
- `--deep` reviews everything three times through narrow lenses (about 3× the sessions). Use it only when asked.

While it runs, check `SLOP status --workdir .slop_scan/<name>` and the log tail every few minutes.
Give the user one line per finished tier, such as "Scouts done: 142/145 (3 failed after retries); architects running".
Failed tasks are retried 3 times, then skipped, and the report flags the gap.

## 3. Present it

When `scan` finishes it prints the grade and the report paths (`<workdir>/reports/`). Read the top
of `SLOP_REPORT.md` (summary, fix-first list, themes, scorecard) and give the user:
- the grade, what set it (`Grade set by:`), and the chief's summary in two sentences
- the fix-first list, as `title (path:lines): why`
- the themes, one line each
- the slop fingerprint's flagged modules and the duplication section's parallel trees, one line each
- the reachability section: how much code nothing checked in launches, and the largest such group
- how many findings were dismissed, how many the appeal pass restored, and the two report paths

If the user disagrees with a call, you may regenerate the chief tier: delete
`<workdir>/tasks/chief` and `<workdir>/out/chief`, then run `SLOP plan chief`, `SLOP run chief`
and `SLOP report`. Don't hand-edit findings.

If the user doubts the scan caught what matters, measure it: have them list the problems they
expect (or plant some in a copy of the code) as `[{"path", "lines": [start, end], "category",
"keywords", "note"}]` (only `path` is required), then run `SLOP recall --workdir <workdir> --expect expected.json`. It reports which
were kept, which were found but dismissed (with the reason), and which were never raised.

## Rules

- This is a maintainability review. If the user wants bugs or security issues, say this skill does not cover them.
- Workers run read-only. Nothing in the scanned repository is modified; all state is under `.slop_scan/<name>/`.
- The grade is the worst of three gates, and the report says which set it:
  - slop index: A < 1, B < 2, C < 4, D < 8, F ≥ 8 weighted findings per 1k LOC (critical 10, high 5,
    medium 2, low 0.5, times confidence 1 / 0.7 / 0.4), counted after the editor, appeal and chief passes;
  - serious findings, whatever the size: 1+ critical caps it at C, 3+ at D; 5+ high at B, 10+ at C, 20+ at D;
  - copied code: 5%+ of non-test lines caps it at B, 10%+ at C, 20%+ at D.
  The bands are provisional; `recall` runs are how to calibrate them.

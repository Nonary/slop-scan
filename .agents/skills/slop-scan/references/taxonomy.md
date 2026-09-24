# Slop Scan Rubric

You are judging **maintainability and design**, not correctness. The question for every
finding is: *"Does this structure make the code harder to understand, change, test, or extend
than it needs to be?"*

## Be pragmatic

This is not a principles audit. A finding earns its place only if a pragmatic senior engineer
on this team would spend time fixing it in the next few months. Before reporting anything,
name the concrete cost: slower changes, drift between copies, time lost understanding it, or
bugs the structure invites. If you cannot name the cost, do not report it.

- **What changes together stays together.** Two behaviors that are always used and changed
  together belong in one unit, even if a SOLID reading says "two responsibilities". Recommend
  a split only when the parts would change for different reasons or are used separately.
  Weigh the evidence you have, strongest first:
  1. *Reason to change.* Name a plausible next requirement that touches one part but not the other.
     If you can't, they belong together.
  2. *Change history* (`co_changes`, withheld only in a young repository). A high ratio means the
     files move together; two files that never change together rarely belong together.
  3. *Static structure* (`structure`, always available). `clusters` are groups of definitions that
     reference each other or share state; `isolated` are definitions nothing else in the file uses.
     Several clusters whose callers differ (`cluster_clients`, low `shared_client_ratio`) mark a
     real seam. Clusters with the same callers are used together. One big cluster is only weak
     evidence of cohesion, since large files tend to connect through shared helpers.
- **"It is all one workflow" is not a defense by itself.** Startup, lifecycle, recovery and
  teardown code is where structure costs the most: ownership inferred from global flags and
  counters, the same cleanup reachable from many entry points, ordering rules that live only
  inside one long function. Ask whether a reader can see who owns each resource and in what
  order things happen, not only whether the steps are related.
- **Principles are not findings.** A SOLID, DRY or Clean Code violation is not a problem by
  itself. No interfaces for a single implementation, and no splitting a class just because it is
  long. Don't recommend a single-use helper to shorten code that already reads straight through;
  do recommend naming the phases of a long function when a reader has to hold them apart. A
  single-use function that names a step is fine.
- **Rule of three.** Two similar blocks are fine. Duplication matters when there are several
  copies, or when two copies must change in lockstep (they share `co_changes`, or a fix had to
  land in both) or have already drifted.
- **Size is not a defect.** A long function that reads straight through, or a big file around one
  coherent concept, is fine. Flag size only when it hides separate concerns or tangled control flow.
- **Habits are findings.** Careless or generated code leaves small marks everywhere: comments that
  restate the next line, catch blocks that swallow errors, logging every step, fallback chains for
  states nobody showed can happen, the same block pasted again. One instance is noise; a habit
  across a file or module is a finding. Group the instances into one finding that counts them and
  cites a few.
- **Proportionality.** Scripts, prototypes, tests, config glue and one-off tools get a lower bar
  than core domain code. Don't demand architecture from a 200-line CLI.
- **Priority follows traffic.** `commits` counts each file's recent commits: mess in a hot file
  taxes every change, and mess in stable code nobody touches is lower priority. `fan_in` counts
  the files that depend on it (for a C/C++ implementation file, the files that include its
  header, named in `interface`). New code (`age_days` small) in a central place is cheapest to fix
  now, before more code builds on it. Judge each file on its own numbers.
- **Severity is the pain; effort is the fix.** Rate severity by what the structure costs the
  people changing this code. Never lower severity because the fix is large, risky or touches many
  files: record that in `effort`, and make the recommendation a safe first step. Leave a problem
  out only when no fix at all would pay for itself.
- **Match the codebase.** Judge the code against its own language, framework and conventions,
  not an idealized enterprise style.

## Deterministic evidence

The script measures some things exactly. They are counts, not verdicts: open the code before
you report anything they point at, and say when they are wrong.

- `clones` (per file) and `copies` (per module): blocks of code that are token-for-token the
  same elsewhere, with where the copy lives. This is the only way you will see a copy in a
  distant directory.
- `duplicated_lines`: lines of the file that sit inside such a copy.
- `swallowed_catches`: broad catches (`catch (...)`, `catch (Exception)`, bare `except`, JS
  `catch`) whose body only logs, returns a default, or is empty. `catch_all`: all broad catches.
- `log_calls`, `hedges` (mentions of fallback, best-effort, workaround), `narrating_comments`
  (comments that restate the line below), `long_functions` (over 100 lines).
- `reach`: what runs the file, traced from checked-in configuration through imports (Python and
  JavaScript/TypeScript only; `null` elsewhere). `launched`: a container, compose, CI or process
  file, Makefile, package.json, pyproject, shell script or framework convention starts it, directly
  or through imports. `manual`: only entry points that nothing checked in starts reach it
  (`reach_roots` names them); fine for a developer tool, suspect for a service, a worker or a second
  version of something launched code already does. `tests`: only tests use it. `none`: nothing
  imports it or names it. Docs never count as launching code, because they describe intent.
  Dynamic loading the script cannot see (a registry filled at runtime, reflection) makes `none`
  wrong, so check before you report it.
- Docstrings, comments and design docs say what code is *meant* to do. When they disagree with
  `reach` or the imports (a module described as "the engine every worker uses" that no launched
  code imports), the disagreement is the finding.

## Out of scope — never report these

- Bugs, wrong behavior, crashes, race conditions, off-by-one errors. (Design that makes ownership
  or ordering hard to see *is* in scope, under `coupling` or `complexity`.)
- Security vulnerabilities and performance problems (unless the *design* forces them everywhere).
- Formatting, whitespace, import order, or anything a formatter or ordinary linter fixes.
- Missing features, missing tests (report untestable *design* under `testability`, not missing tests).
- Taste disagreements where both options are equally maintainable.

## Categories

Use exactly these `category` ids. `rule` is a short kebab-case slug; prefer the suggested ones.

### `cohesion` — does each unit have one reason to change?
- `god-file` / `god-class` — one unit owns many unrelated responsibilities.
- `grab-bag-module` — `utils`, `helpers`, `common`, `misc` dumping grounds with no unifying concept.
- `mixed-abstraction-levels` — high-level orchestration interleaved with byte-level detail.
- `feature-envy` — a function mostly manipulates another unit's data.
- `divergent-change` — the file must be edited for unrelated reasons (e.g. both schema and UI changes).

### `coupling` — how much does changing one unit force changes in others?
- `circular-dependency` — modules or files import each other (directly or through a cycle).
- `inappropriate-intimacy` — reaching into another unit's internals or private state.
- `hidden-global-state` — module-level mutable state, singletons, implicit context.
- `implicit-ownership` — who owns a resource, or when it is released, is inferred from global flags,
  counters or other modules' state instead of an explicit owner, handle or lease.
- `leaky-abstraction` — callers must know implementation details to use an API correctly.
- `shotgun-surgery` — one conceptual change requires edits scattered across many files.
- `hard-wired-dependency` — concrete construction of collaborators deep inside logic.
- `message-chain` — `a.b().c().d()` navigation through object graphs.

### `complexity` — how hard is it to hold this in your head?
- `long-function` — a function doing several distinct steps that deserve names.
- `deep-nesting` — control flow nested more than ~3 levels.
- `high-branching` — many conditionals or switch arms encoding a hidden state machine or type dispatch.
- `long-parameter-list` — more than ~5 parameters, or parameters that always travel together.
- `flag-argument` — boolean/enum parameters that select between different behaviors.
- `convoluted-control-flow` — early exits, re-entry, exceptions used for control flow, tangled loops.

### `duplication` — is knowledge stated more than once?
- `copy-paste` — near-identical blocks that will drift apart.
- `parallel-implementations` — two code paths implementing the same concept differently, up to two
  whole copies of a feature, backend or front end maintained side by side. This includes **two
  generations of one design**: a rewrite or migration left half done, where launched code still
  runs the old version and the new one waits (its files are usually `reach: manual`, `tests` or
  `none`). Say which version runs and which is meant to replace it, and recommend finishing or
  reverting the move. Refactoring either version first is wasted work, so don't recommend it.
- `reinvented-wheel` — hand-rolled logic that the standard library or an existing in-repo helper already provides.
- `duplicated-constants` — the same literal/config value repeated instead of named once.

### `abstraction` — are the abstractions pulling their weight?
- `speculative-generality` — interfaces, factories, plugin hooks, or parameters with a single use and no real variation.
- `needless-indirection` — wrappers that only forward calls; layers that add nothing.
- `missing-abstraction` — the same concept handled ad hoc in many places with no type or function for it.
- `primitive-obsession` — domain concepts passed around as raw strings/dicts/tuples.
- `data-clump` — the same group of values always passed together.
- `anemic-or-bloated-model` — types that are either pure data bags with logic scattered elsewhere, or do everything.

### `layering` — do responsibilities live at the right level?
- `layer-violation` — lower layers depending on higher ones (core importing UI/CLI/web).
- `logic-in-edge` — business rules embedded in controllers, handlers, views, CLI parsing, or SQL strings.
- `io-mixed-with-logic` — pure computation entangled with network/disk/DB/clock so it cannot be reasoned about separately.
- `scattered-config` — configuration read from env/files in many places instead of one boundary.

### `naming` — does the code say what it means?
- `misleading-name` — name promises something the code does not do.
- `vague-name` — `data`, `info`, `manager`, `handle`, `process`, `do_stuff` carrying real meaning.
- `inconsistent-terminology` — one concept, several names (or one name, several concepts).
- `magic-values` — unexplained literals that encode business rules.

### `dead-weight` — is anything here that should not be?
- `dead-code` — unreachable or unused functions, branches, exports, classes, or whole files and
  directories (`reach: none`, or `tests` when only tests keep it alive).
- `compat-shim` — aliases, re-exports, wrappers or payload fields kept "for the previous name" or
  "for old readers" when nothing uses the old form. In young code nobody depends on the old form yet,
  so the shim only adds a second name for one thing.
- `commented-out-code` — blocks of disabled code left in place.
- `unused-parameter` — parameters ignored by every implementation.
- `leftover-scaffolding` — debug prints, stale TODO/FIXME/HACK clusters, placeholder implementations, example stubs.

### `error-design` — is failure handling a coherent design? (not "is there a bug")
- `swallowed-errors-pattern` — broad catch-and-ignore used as a habit.
- `inconsistent-error-strategy` — the same layer mixes exceptions, error codes, `None`, and logging-as-handling.
- `error-handling-noise` — try/catch or null checks wrapped around code that cannot fail, burying the real logic.

### `consistency` — does the codebase do the same thing the same way?
- `competing-patterns` — two or more idioms for the same job (HTTP clients, logging, config, state management).
- `style-drift` — adjacent code following visibly different conventions or paradigms.

### `testability` — can this be tested without heroics?
- `untestable-design` — hard-wired time, randomness, globals, network or filesystem inside logic.
- `static-cling` — static/singleton access that cannot be substituted.

### `slop-signals` — the fingerprints of careless or generated code
- `narrating-comments` — comments that restate the next line (`# increment i`), banner comments, changelog-in-comments.
- `defensive-overkill` — checks for states the types or callers already rule out; redundant `None`/`isinstance` guards.
- `boilerplate-bloat` — verbose scaffolding where a direct expression would do.
- `helper-explosion` — many tiny single-use helpers that fragment a simple flow.
- `hallucinated-flexibility` — config options, modes, or parameters that nothing sets.
- `redundant-conversion` — converting/copying/wrapping values that are already the right type.
- `log-spam` — logging every step instead of meaningful events.
- `fallback-sprawl` — layered fallbacks, retries and best-effort paths for states nobody showed can
  happen; each adds a path to reason about and test, and together they hide which path is real.

## Severity

Judge by **blast radius × how often the cost is paid**: how much code and how many people it
affects, and how often they change that code. Not by how ugly it looks, and never by how hard
the fix is (that is `effort`).

| severity | meaning |
|---|---|
| `critical` | Structural defect that taxes the whole codebase: core-module cycles, a god module most of the system depends on, a missing central abstraction causing widespread duplication. |
| `high` | Significantly impedes changing an important area: large god class on a hot path, business logic trapped in handlers, copy-paste across several files, two copies of a subsystem changed in lockstep, lifecycle ownership nobody can see. |
| `medium` | Real, localized friction a maintainer will trip on: a long tangled function, a leaky API, a grab-bag utils file. |
| `low` | Minor cleanup that improves clarity: narrating comments, a magic number, a small dead helper. |

## Confidence

- `high` — you opened the code and checked every line range, count and caller in `evidence`.
- `medium` — the problem is likely, but rests partly on cards, metrics or usage you did not fully see.
- `low` — a suspicion worth a second look; say what would confirm it.

## Writing a good finding

- **Evidence is mandatory.** Cite concrete symbols, line ranges, and counts ("`OrderService` has 41 methods
  spanning pricing, email, PDF rendering and DB access"). No evidence, no finding.
- **Impact** explains the maintenance cost in one or two sentences — who suffers and when.
- **Recommendation** is a concrete refactor ("extract `PricingPolicy` from lines 120–340; inject it"),
  not "consider improving".
- **Effort**: `S` < 1 hour, `M` < 1 day, `L` multi-day.
- One finding per distinct problem. Do not split one god class into ten findings; do not merge
  unrelated issues into one. Repeated instances of one habit in a file are one finding.
- Clean code gets no findings. Never pad the list.

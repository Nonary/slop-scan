# Slop Scan Output Contracts

Every worker answers with exactly one JSON object as its final message: no prose and no code
fences. Paths are **relative to the scanned root**; line numbers are 1-based. Malformed
findings are discarded, so an item with a bad path or category is simply lost.

## Lenses

Tier-1, tier-2 and tier-3 tasks have a `lens`: `{name, categories, focus, primary}`.

- Report only findings whose `category` is in `lens.categories`. With the default `all` lens
  that is every category. With a narrower lens, other agents cover the rest, so ignore it.
- Use `lens.focus` to decide what to look for first.
- Only the **primary** lens writes summaries (file cards, module summaries, architecture summary,
  themes and flows). Other lenses write `[]` for those lists and omit those fields.

## Finding

```json
{
  "category": "cohesion",
  "rule": "god-class",
  "severity": "high",
  "confidence": "high",
  "title": "OrderService mixes pricing, email, PDF rendering and persistence",
  "locations": [{"path": "src/orders/service.py", "start_line": 12, "end_line": 480}],
  "evidence": "41 methods; pricing (L40-190), SMTP (L200-260), PDF (L270-400), raw SQL (L410-480). Pricing and PDF are separate clusters with different callers.",
  "impact": "Every pricing change risks email and PDF code; the class cannot be tested without a DB and SMTP.",
  "recommendation": "Extract PricingPolicy and InvoiceRenderer; keep OrderService as a thin coordinator.",
  "effort": "L"
}
```

- `category`: one of `cohesion`, `coupling`, `complexity`, `duplication`, `abstraction`, `layering`,
  `naming`, `dead-weight`, `error-design`, `consistency`, `testability`, `slop-signals`.
- `rule`: a kebab-case slug, preferably one from the rubric.
- `severity`: `critical` | `high` | `medium` | `low`. `confidence`: `high` | `medium` | `low`. `effort`: `S` | `M` | `L`.
- `locations`: 1 or more entries, primary first. `start_line`/`end_line` may be `null` for
  whole-file or whole-module findings (a module is its directory path, `.` for the root).

## Tier 1: scout

```json
{
  "files": [
    {
      "path": "src/orders/service.py",
      "purpose": "One sentence: what this file is for.",
      "responsibilities": ["pricing rules", "order persistence"],
      "key_symbols": ["OrderService", "calculate_total"],
      "collaborators": "What it depends on and what depends on it, in plain words.",
      "health": "sloppy",
      "notes": "Optional cross-file suspicions for the module reviewer."
    }
  ],
  "findings": []
}
```

- Primary lens: one card for **every** file in the task, clean ones included. Other lenses: `"files": []`.
- `health`: `clean` | `minor` | `concerning` | `sloppy`.
- Each finding's first location must be a file in this task, inside its segment if it has one.
  Cross-file suspicions go in `notes`; the tier-2 reviewer handles them.

## Tier 2: architect

```json
{
  "modules": [
    {
      "module": "src/orders",
      "summary": "What this module is responsible for, in 1-3 sentences.",
      "responsibilities": ["order lifecycle", "pricing"],
      "cohesion": "low",
      "coupling": "high",
      "health": "concerning",
      "notes": "Optional observations for the systems reviewer."
    }
  ],
  "findings": []
}
```

- Primary lens: one entry for **every** module in the task. Other lenses: `"modules": []`.
- `cohesion` / `coupling`: `high` | `medium` | `low`. `health`: as in tier 1.
- Report only what is visible across files. Do not restate tier-1 findings.
- A large module may be split into parts (`part: {number, of}`). Your `files` are your part;
  `rest_of_module` lists the others with one-line purposes.

## Tier 3: systems

```json
{
  "architecture_summary": "How the system is organized, in one paragraph.",
  "themes": [{"title": "Business rules leak into HTTP handlers", "description": "Where, how widespread, why it matters."}],
  "flows": [
    {
      "name": "Order cancellation",
      "why": "Refunds, stock release and email are triggered from the API, a cron job and the payment webhook; who undoes what is unclear.",
      "entry_points": [{"path": "src/orders/service.py", "symbol": "cancel_order"}, {"path": "src/payments/webhooks.py", "symbol": "on_refund"}]
    }
  ],
  "findings": []
}
```

- Primary lens only: `architecture_summary`, 1-8 `themes` and 2-6 `flows`. Other lenses: `findings` only.
- `flows`: the cross-module workflows most worth tracing end to end (startup, a core request,
  recovery, shutdown or teardown), each with 1-6 `entry_points`: a scanned file and the name of a
  function or method in it. A separate worker traces each one.

## Flow tracer

```json
{
  "flow": {
    "name": "Order cancellation",
    "summary": "How the flow works end to end, in 2-5 sentences.",
    "steps": ["src/api/orders.py:212 cancel endpoint: calls OrderService.cancel_order", "..."],
    "ownership": "Who owns each resource along the way and who releases it, or why that is unclear.",
    "health": "concerning"
  },
  "findings": []
}
```

- `steps`: the path through the code in order, one `path:line symbol: what happens` per step.
- `health`: `clean` | `minor` | `concerning` | `sloppy`.
- `findings`: any category; locations may be in any scanned file.

## Editor

```json
{
  "decisions": [
    {"finding_id": "SLOP-3fa91c2e", "action": "keep", "rationale": "Checked L40-480; pricing and PDF change independently."},
    {"finding_id": "SLOP-77b0d1aa", "action": "rerate", "severity": "low", "rationale": "Two call sites, rarely changed."},
    {"finding_id": "SLOP-0c2e9b13", "action": "merge", "merge_into": "SLOP-3fa91c2e", "rationale": "Same god class, reported twice."},
    {"finding_id": "SLOP-91aa0f7d", "action": "drop", "rationale": "Parse and validate always change together; one unit is right."}
  ]
}
```

- One decision for **every** finding in the task. `action`: `keep` | `rerate` (with a new
  `severity`, higher or lower) | `merge` (with `merge_into`: another finding id in this batch) | `drop`.

## Appeal

```json
{
  "decisions": [
    {"finding_id": "SLOP-3fa91c2e", "action": "overturn", "severity": "high", "rationale": "Dropped as 'one cancellation workflow', but refund state is inferred from three flags set in two other modules, and cancel is called from four files."},
    {"finding_id": "SLOP-77b0d1aa", "action": "uphold", "rationale": "A 90-line release script with two commits this year; the downgrade matches its traffic."}
  ]
}
```

- One decision for **every** finding in the task. `action`: `uphold` (the editor's call stands) |
  `overturn` (with the `severity` the finding deserves; the finding is restored if it was dropped).

## Chief

```json
{
  "executive_summary": "Three to six sentences: overall health, what drives it, what to do first.",
  "themes": [{"title": "...", "description": "..."}],
  "priorities": [{"finding_id": "SLOP-3fa91c2e", "why": "Hot file (48 commits); every pricing change touches it."}],
  "drops": [{"finding_id": "SLOP-5d0e11c4", "rationale": "Style preference; the two helpers are fine as they are."}]
}
```

- `themes`: 1-8, the final list; rewrite the tier-3 themes as needed.
- `priorities`: 5-15 findings to fix first, most valuable first.
- `drops`: findings that should not be in the report at all. May be empty.

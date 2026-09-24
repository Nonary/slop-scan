"""Lenses split each review into several narrow passes over the same code.

Cheap models do their best work on short, focused tasks, so instead of one agent judging every
category at once, each lens gets its own agent that may only report its own categories. One
lens per tier is the "primary" lens and also writes the summaries (file cards, module
summaries, architecture summary and themes) that later tiers build on.
"""

from dataclasses import asdict, dataclass

from schema import CATEGORIES


@dataclass(frozen=True)
class Lens:
    name: str
    categories: tuple
    focus: str
    primary: bool = False


_ALL = Lens("all", tuple(sorted(CATEGORIES)), "Every category in the rubric.", primary=True)

LENSES = {
    "tier1": (
        Lens("design", ("cohesion", "complexity", "abstraction", "layering"),
             "Is each file and function doing one job at one level of abstraction? God classes, long or deeply "
             "nested functions, flag arguments, speculative or missing abstractions, logic in the wrong layer.",
             primary=True),
        Lens("hygiene", ("naming", "dead-weight", "slop-signals", "error-design"),
             "Is the code honest and lean? Misleading or vague names, magic values, dead or commented-out code, "
             "leftover scaffolding, narrating comments, defensive overkill, boilerplate, swallowed errors."),
        Lens("dependencies", ("coupling", "duplication", "consistency", "testability"),
             "How does this code lean on everything else? Reaching into internals, hidden global state, hard-wired "
             "collaborators, copy-paste, reinvented helpers, competing idioms, designs that cannot be tested."),
    ),
    "tier2": (
        Lens("boundaries", ("cohesion", "layering", "abstraction", "complexity"),
             "Does the module hold one concept with a clear boundary? Grab-bag directories, files doing each "
             "other's jobs, logic leaking into edges, missing or needless abstraction layers.",
             primary=True),
        Lens("coupling", ("coupling", "testability", "error-design", "dead-weight"),
             "How tangled is the module with its neighbours? Heavy or cyclic dependencies, intimacy with other "
             "modules' internals, shotgun surgery, shared mutable state, inconsistent error contracts, unused files."),
        Lens("duplication", ("duplication", "consistency", "naming", "slop-signals"),
             "Do sibling files repeat each other or disagree? Parallel implementations, copy-paste across files, "
             "competing patterns, one concept under several names, the same boilerplate stamped everywhere."),
    ),
    "tier3": (
        Lens("architecture", ("layering", "coupling", "cohesion"),
             "How is the system organized and where does that organization break down? Module cycles, upward "
             "dependencies, god modules, missing layers. Also write the architecture summary and the themes, "
             "and nominate the flows to trace.",
             primary=True),
        Lens("reuse", ("duplication", "abstraction"),
             "Where is the same concept built more than once across modules, and which shared abstraction is missing?"),
        Lens("conventions", ("consistency", "error-design", "testability", "naming", "slop-signals"),
             "Does the codebase do the same thing the same way everywhere? Competing HTTP, config, logging, error "
             "and state patterns; inconsistent terminology; codebase-wide slop habits."),
    ),
}


def lenses_for(tier, names):
    """names: list of lens names, or ['all'] for a single combined pass."""
    if names == ["all"]:
        return (_ALL,)
    available = {lens.name: lens for lens in LENSES[tier]}
    unknown = [n for n in names if n not in available]
    if unknown:
        raise SystemExit(f"Unknown {tier} lens(es) {unknown}; choose from {sorted(available)} or 'all'.")
    chosen = tuple(available[n] for n in names)
    if not any(lens.primary for lens in chosen):
        primary = next(lens for lens in LENSES[tier] if lens.primary)
        raise SystemExit(f"The {tier} lenses must include '{primary.name}', which writes the summaries later tiers need.")
    return chosen


def default_names(tier):
    return [lens.name for lens in LENSES[tier]]


def lens_payload(lens):
    return asdict(lens)

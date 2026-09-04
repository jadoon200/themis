"""What a worker is allowed to do, declared rather than assumed.

A single worker that runs every stage needs, all at once: git, dbt, warehouse
credentials with write access, a language model, and the database. That is a large
surface for a process whose main job is parsing SQL, and in a financial environment
the interesting question about a review tool is not what it does but what it *can*
do.

So each capability is named, and a worker claims only the work its capabilities cover.
The one that matters is ``EXECUTE``: it is the only stage that runs code against a
warehouse, and a fleet where most workers cannot do it at all is a different security
story from one where they merely choose not to. Analysis workers need no credentials,
so they cannot leak or misuse any.

This is enforced twice on purpose — once when claiming work, so a run needing
execution is never handed to a worker that cannot do it, and once at the point of
execution, so a run that reaches the wrong worker anyway refuses instead of running.
A guard that exists only in the scheduler is a guard that a bug in the scheduler
removes.
"""

from __future__ import annotations

from enum import StrEnum


class Capability(StrEnum):
    """One kind of work, and the access it implies."""

    # Parse, diff, derive grain, trace lineage, run the rules. Pure CPU: no warehouse,
    # no credentials, no model. The bulk of the value, and the cheapest to run.
    ANALYSE = "analyse"
    # Run `dbt compile` to produce a manifest. Needs the project, dbt, and warehouse
    # credentials.
    #
    # Not read-only, despite the name of the command. `run_query` executes during
    # compilation, and dbt-core issue 12447 records pre-hooks running their SQL under
    # `dbt compile` even when guarded by `{% if execute %}` — a hook containing a
    # DELETE will run. The target allowlist is what keeps that away from production;
    # the capability itself does not, and describing it as read-only would be a
    # security claim this cannot support.
    COMPILE = "compile"
    # Stage 3. Builds both revisions against a warehouse. The only capability that
    # writes anything anywhere, and the only one that needs credentials.
    EXECUTE = "execute"
    # The specialists and supervisor. Needs a model endpoint and nothing else.
    REVIEW = "review"


# What a worker gets when nothing is said. Deliberately excludes EXECUTE: turning on
# warehouse access should be a thing someone did, not a thing they failed to prevent.
DEFAULT_CAPABILITIES: frozenset[Capability] = frozenset(
    {Capability.ANALYSE, Capability.COMPILE, Capability.REVIEW}
)


class CapabilityError(RuntimeError):
    """Work was attempted that this worker is not permitted to do."""


def parse_capabilities(raw: str | None) -> frozenset[Capability]:
    """Read a comma-separated capability list.

    ``"all"`` is spelled out rather than implied by an empty value, so a fleet running
    with warehouse access is running with it on purpose.
    """
    if raw is None or not raw.strip():
        return DEFAULT_CAPABILITIES
    if raw.strip().lower() == "all":
        return frozenset(Capability)

    wanted: set[Capability] = set()
    for part in raw.split(","):
        name = part.strip().lower()
        if not name:
            continue
        try:
            wanted.add(Capability(name))
        except ValueError as exc:
            known = ", ".join(c.value for c in Capability)
            raise CapabilityError(f"unknown capability {name!r}; known: {known}") from exc
    if not wanted:
        return DEFAULT_CAPABILITIES
    return frozenset(wanted)


def require(held: frozenset[Capability], needed: Capability, *, what: str) -> None:
    """Refuse work outside what this worker holds.

    Raises rather than degrading. A review that quietly skipped execution because the
    worker could not do it would report inference as though nothing had been left out,
    and a reviewer reading "no findings" would have no way to know.
    """
    if needed not in held:
        raise CapabilityError(
            f"{what} needs the {needed.value!r} capability; this worker holds "
            f"{', '.join(sorted(c.value for c in held)) or 'none'}"
        )

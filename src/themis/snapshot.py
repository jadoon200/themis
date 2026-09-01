"""``ProjectSnapshot`` — the single abstraction the pipeline codes against.

Three acquire backends fill this to different depths (see ``themis.acquire``). Every
field that can be partially populated carries enough information for a rule to tell
whether it may rely on it. The alternative — rules silently reasoning over absent data
— is how a reviewer of financial code ends up confidently wrong.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from themis.models import Backend


class ColumnSchema(BaseModel):
    model_config = {"frozen": True}

    name: str
    data_type: str | None = None
    description: str | None = None


class DeclaredTest(BaseModel):
    """A test declared in schema.yml.

    Expected to be near-empty on the target project — which is precisely why grain is
    derived rather than read. Kept because the demo project has a tested variant, and
    because tests THEMIS suggests get validated through this same shape.
    """

    model_config = {"frozen": True}

    test_name: str
    model_name: str
    columns: tuple[str, ...] = ()
    severity: str = "error"


class ModelNode(BaseModel):
    """One dbt model as THEMIS sees it."""

    name: str
    unique_id: str
    file_path: str
    resource_type: str = "model"
    raw_sql: str = ""
    # Only populated from a *compiled* manifest. With heavy macro usage, raw_sql is
    # close to unanalysable, so most of the pipeline requires this to be present.
    compiled_sql: str | None = None
    materialization: str = "view"
    incremental_strategy: str | None = None
    unique_key: tuple[str, ...] = ()
    on_schema_change: str | None = None
    tags: tuple[str, ...] = ()
    meta: dict[str, str] = Field(default_factory=dict)
    columns: tuple[ColumnSchema, ...] = ()
    contract_enforced: bool = False
    depends_on_models: tuple[str, ...] = ()
    depends_on_macros: tuple[str, ...] = ()
    depends_on_sources: tuple[str, ...] = ()

    @property
    def is_seed(self) -> bool:
        """Seeds are CSV data, not SQL. Their grain can be measured but never derived."""
        return self.resource_type == "seed"

    @property
    def analysable_sql(self) -> str | None:
        """The SQL a parser can actually reason about.

        Returns None rather than falling back to raw_sql: silently parsing
        Jinja-laden source would produce confident findings from a misread AST.
        """
        return self.compiled_sql


class MacroNode(BaseModel):
    """A dbt macro. Changes here fan out to every model that calls it."""

    model_config = {"frozen": True}

    name: str
    unique_id: str
    file_path: str
    raw_sql: str
    # Macros call other macros. A helper that no model references directly still
    # reaches them through its callers, so the impact walk has to be transitive.
    depends_on_macros: tuple[str, ...] = ()


class Exposure(BaseModel):
    """A downstream consumer — dashboard, report, or regulatory submission.

    Anything reaching one of these is escalated automatically.
    """

    model_config = {"frozen": True}

    name: str
    exposure_type: str
    depends_on: tuple[str, ...] = ()
    owner: str | None = None


class ProjectSnapshot(BaseModel):
    """A dbt project at one revision, as fully as the backend could resolve it."""

    revision: str
    backend: Backend
    models: dict[str, ModelNode] = Field(default_factory=dict)
    macros: dict[str, MacroNode] = Field(default_factory=dict)
    tests: tuple[DeclaredTest, ...] = ()
    exposures: dict[str, Exposure] = Field(default_factory=dict)
    # model name -> direct children, from the manifest's child_map where available.
    child_map: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @property
    def has_compiled_sql(self) -> bool:
        """Whether the analysis stages can do their real work.

        False means the manifest came from `dbt parse` rather than `dbt compile`, and
        the pipeline should say so loudly instead of degrading in silence.
        """
        return any(m.compiled_sql is not None for m in self.models.values())

    def downstream_of(self, model_name: str, *, depth: int = 10) -> tuple[str, ...]:
        """Every model reachable downstream, breadth-first with a cycle guard."""
        seen: set[str] = set()
        frontier = [model_name]
        for _ in range(depth):
            nxt: list[str] = []
            for node in frontier:
                for child in self.child_map.get(node, ()):
                    if child not in seen:
                        seen.add(child)
                        nxt.append(child)
            if not nxt:
                break
            frontier = nxt
        return tuple(sorted(seen))

    def macro_closure(self, macro_name: str, *, depth: int = 10) -> set[str]:
        """A macro plus every macro that transitively calls it.

        ``minor_to_major`` may appear in no model's dependency list while still
        reaching a dozen models through ``signed_amount``. Walking only direct
        references would report that edit as touching nothing.
        """
        callers = {macro_name}
        for _ in range(depth):
            grown = False
            for macro in self.macros.values():
                if macro.name in callers:
                    continue
                referenced = {ref.split(".")[-1] for ref in macro.depends_on_macros}
                if referenced & callers:
                    callers.add(macro.name)
                    grown = True
            if not grown:
                break
        return callers

    def models_using_macro(self, macro_name: str) -> tuple[str, ...]:
        """Models a macro edit reaches, directly or through other macros.

        The reason a one-line macro edit is analysed as the N-model change it is,
        rather than the one-file change a diff makes it look like.
        """
        callers = self.macro_closure(macro_name)
        return tuple(
            sorted(
                name
                for name, model in self.models.items()
                if {ref.split(".")[-1] for ref in model.depends_on_macros} & callers
            )
        )

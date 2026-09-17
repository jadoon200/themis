"""A synthetic dbt project large enough to find out where THEMIS stops scaling.

The demo project has eighteen models. A bank's warehouse project has hundreds or
thousands, and nothing about eighteen says how grain inference, column lineage, rule
contexts or volatility detection behave at that size — or which of them is quadratic.

This builds compiled snapshots directly, with no dbt and no warehouse, in the shapes a
real project has: sources, staging models that cast and stamp audit columns, intermediate
models that join two or three staging models through CTEs and aggregate, and marts that
aggregate intermediates, sometimes through a UNION ALL or a window function. The SQL is
what dbt compiles to — fully qualified relations, Trino dialect — because every stage
parses it, and a generator that emitted simpler SQL would measure a simpler tool.

Deterministic for a given seed, so a timing on one machine can be compared with the next.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from themis.acquire.git import ChangedFile
from themis.acquire.snapshot_builder import AcquireResult
from themis.models import Backend
from themis.snapshot import ModelNode, ProjectSnapshot

_DATABASE = '"warehouse"'
_SCHEMA = '"analytics"'
_PROJECT = "synthetic"


def _relation(name: str) -> str:
    return f'{_DATABASE}.{_SCHEMA}."{name}"'


@dataclass(frozen=True)
class Shape:
    """How the models divide across layers, as fractions of the total."""

    staging: float = 0.40
    intermediate: float = 0.35
    # The remainder are marts.


def _staging(i: int, source: str, rng: random.Random) -> str:
    audit = ",\n    current_timestamp as _loaded_at" if rng.random() < 0.6 else ""
    return (
        "select\n"
        "    cast(id as varchar) as entity_id,\n"
        "    cast(account_id as varchar) as account_id,\n"
        "    cast(posted_on as date) as posted_on,\n"
        "    date_trunc('month', cast(posted_on as date)) as period_month,\n"
        "    upper(currency_code) as currency_code,\n"
        "    cast(amount_minor as decimal(38, 2)) / 100 as amount,\n"
        f"    status_{i % 7} as status,\n"
        "    coalesce(region, 'unknown') as region"
        f"{audit}\n"
        f"from {_relation(source)}\n"
        "where status is not null"
    )


def _intermediate(i: int, parents: list[str], rng: random.Random) -> str:
    ctes = ",\n".join(
        f"p{n} as (\n    select * from {_relation(parent)}\n)" for n, parent in enumerate(parents)
    )
    joins = "\n".join(
        f"    left join p{n} on p0.account_id = p{n}.account_id "
        f"and p0.period_month = p{n}.period_month"
        for n in range(1, len(parents))
    )
    extra = " + ".join(f"coalesce(p{n}.amount, 0)" for n in range(1, len(parents))) or "0"
    if rng.random() < 0.5:
        body = (
            "select\n"
            "    p0.account_id,\n"
            "    p0.period_month,\n"
            "    count(*) as entry_count,\n"
            f"    sum(p0.amount + {extra}) as net_amount\n"
            "from p0\n"
            f"{joins}\n"
            f"where p0.region <> 'excluded_{i % 5}'\n"
            "group by p0.account_id, p0.period_month"
        )
    else:
        body = (
            "select\n"
            "    p0.entity_id,\n"
            "    p0.account_id,\n"
            "    p0.period_month,\n"
            f"    p0.amount + {extra} as net_amount,\n"
            "    row_number() over (partition by p0.account_id order by p0.posted_on) as seq\n"
            "from p0\n"
            f"{joins}"
        )
    return f"with {ctes},\n\nshaped as (\n{body}\n)\n\nselect * from shaped"


def _mart(i: int, parents: list[str], rng: random.Random) -> str:
    if len(parents) > 1 and rng.random() < 0.3:
        union = "\n    union all\n".join(
            f"    select account_id, period_month, net_amount from {_relation(parent)}"
            for parent in parents
        )
        source = f"(\n{union}\n) as unioned"
    else:
        source = f"{_relation(parents[0])} as base"
    return (
        "select\n"
        "    account_id,\n"
        "    period_month,\n"
        "    sum(net_amount) as reported_amount,\n"
        f"    count(*) as rows_{i % 3}\n"
        f"from {source}\n"
        "group by account_id, period_month"
    )


def _node(name: str, folder: str, sql: str, parents: list[str], **extra: object) -> ModelNode:
    return ModelNode(
        name=name,
        unique_id=f"model.{_PROJECT}.{name}",
        file_path=f"models/{folder}/{name}.sql",
        relation_name=_relation(name),
        raw_sql=sql,
        compiled_sql=sql,
        depends_on_models=tuple(f"model.{_PROJECT}.{p}" for p in parents),
        **extra,  # type: ignore[arg-type]
    )


_DEFAULT_SHAPE = Shape()


def project(models: int, *, seed: int = 7, shape: Shape = _DEFAULT_SHAPE) -> ProjectSnapshot:
    """A compiled snapshot with ``models`` models, plus the sources they read."""
    rng = random.Random(seed)
    n_staging = max(1, int(models * shape.staging))
    n_intermediate = max(1, int(models * shape.intermediate))
    n_marts = max(1, models - n_staging - n_intermediate)

    nodes: dict[str, ModelNode] = {}
    sources = [f"raw_{i}" for i in range(max(1, n_staging // 2))]
    for source in sources:
        nodes[source] = ModelNode(
            name=source,
            unique_id=f"seed.{_PROJECT}.{source}",
            file_path=f"seeds/{source}.csv",
            resource_type="seed",
            relation_name=_relation(source),
        )

    staging = [f"stg_{i}" for i in range(n_staging)]
    for i, name in enumerate(staging):
        source = sources[i % len(sources)]
        nodes[name] = _node(name, "staging", _staging(i, source, rng), [source])
        nodes[name] = nodes[name].model_copy(
            update={"depends_on_models": (f"seed.{_PROJECT}.{source}",)}
        )

    intermediate = [f"int_{i}" for i in range(n_intermediate)]
    for i, name in enumerate(intermediate):
        parents = rng.sample(staging, k=min(len(staging), rng.randint(1, 3)))
        nodes[name] = _node(name, "intermediate", _intermediate(i, parents, rng), parents)

    for i in range(n_marts):
        name = f"fct_{i}"
        pool = intermediate if intermediate else staging
        parents = rng.sample(pool, k=min(len(pool), rng.randint(1, 3)))
        tags = ("regulatory",) if rng.random() < 0.1 else ()
        nodes[name] = _node(
            name, "marts", _mart(i, parents, rng), parents, materialization="table", tags=tags
        )

    child_map: dict[str, list[str]] = {name: [] for name in nodes}
    for name, node in nodes.items():
        for dependency in node.depends_on_models:
            parent = dependency.split(".")[-1]
            child_map.setdefault(parent, []).append(name)

    return ProjectSnapshot(
        revision="synthetic",
        backend=Backend.MANIFEST,
        models=nodes,
        child_map={name: tuple(sorted(children)) for name, children in child_map.items()},
    )


def changed(before: ProjectSnapshot, *, count: int, seed: int = 11) -> AcquireResult:
    """``before`` with ``count`` staging models edited the way a real change edits them.

    Staging, because that is where a change reaches the most of the project — the case
    that decides whether a review of one small edit finishes.
    """
    rng = random.Random(seed)
    staging = sorted(name for name in before.models if name.startswith("stg_"))
    edited = rng.sample(staging, k=min(count, len(staging)))
    models = dict(before.models)
    for name in edited:
        node = models[name]
        sql = (node.compiled_sql or "").replace(
            "where status is not null", "where status is not null and amount > 0"
        )
        models[name] = node.model_copy(update={"compiled_sql": sql, "raw_sql": sql})
    after = before.model_copy(update={"models": models, "revision": "synthetic-head"})
    return AcquireResult(
        before=before,
        after=after,
        changed=tuple(ChangedFile(path=models[name].file_path, status="M") for name in edited),
    )

"""The grain of a seed, read from the CSV that defines it.

Every other tool infers a key from something the project declares — a uniqueness test, a
contract, a primary-key constraint. dbt-core's own proposal works that way and so does
Datafold's. The projects this is built for declare none of it, which is the founding
constraint here, and it left seeds as the one place a grain could not be derived at all:
"seed data, not SQL — grain cannot be derived, only measured".

But a seed *is* data, and it is in the repository. So it can be measured without a
warehouse, without a build, and without asking anyone: read the file and find which columns
identify a row. That is not an inference, and it matters more than it sounds — in a
financial project seeds are reference data, the mappings and hierarchies that every join
lands on, and whether a join onto one fans out is exactly what F1 spends its time deciding.

Bounded on purpose: single columns first, then pairs, and only over a slice of the file.
A key is reported only if it is unique across every row read, and the row cap is reported
with it, because "unique in the first 50,000 rows" is a different claim from "unique".
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass

from themis.vocabulary import DEFAULT as DEFAULT_VOCABULARY
from themis.vocabulary import Vocabulary

# A seed large enough to matter is rare — dbt seeds are meant for reference data — but the
# cap keeps a mistaken 200MB CSV from stalling a review.
MAX_ROWS = 50_000
# Pairs only. Triples are a combinatorial jump for a key nobody would recognise as one, and
# a composite of three reference columns is better answered by measuring the built table.
MAX_COMBINATION = 2


# A continuous measurement is not an identifier, however unique it happens to be. The FX
# seed here has thirty distinct rates and a real key of (currency_code, rate_date): taking
# `rate` would have handed every later stage a key that pairs rows which are not the same
# row, and proved a join safe on a column nobody would ever join on.
_DECIMAL = re.compile(r"^-?\d+\.\d+$")
_NUMERIC = re.compile(r"^-?\d+(\.\d+)?$")


def _is_measurement(column: str, values: list[str], vocabulary: Vocabulary) -> bool:
    """Whether this column holds a quantity rather than an identifier.

    The name alone is not enough, and the way it fails is instructive: `rate_date` matches
    the money vocabulary because a rate is money, and excluding it left the FX seed with no
    key at all when its real one is (currency_code, rate_date). So a monetary *name* only
    disqualifies a column whose values are also numbers.
    """
    present = [value for value in values if value != ""]
    if not present:
        return False
    numeric = all(_NUMERIC.match(value) for value in present)
    if vocabulary.is_monetary(column) and numeric:
        return True
    return all(_DECIMAL.match(value) for value in present)


@dataclass(frozen=True)
class SeedKey:
    """Columns that identify a row in a seed, and how much of it was read."""

    columns: tuple[str, ...]
    rows_read: int
    complete: bool  # every row was read, so uniqueness is over the whole seed


def _rows(text: str, limit: int) -> tuple[list[str], list[list[str]]]:
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return [], []
    rows: list[list[str]] = []
    for row in reader:
        if len(rows) >= limit:
            break
        # A short row is padded rather than skipped: a trailing empty field is missing
        # from some exports, and dropping the row would change what "unique" means.
        if len(row) < len(header):
            row = [*row, *[""] * (len(header) - len(row))]
        rows.append(row)
    return header, rows


def seed_key(
    text: str,
    *,
    max_rows: int = MAX_ROWS,
    vocabulary: Vocabulary = DEFAULT_VOCABULARY,
) -> SeedKey | None:
    """The smallest combination of columns unique across the rows read, or None.

    Single columns before pairs, and left to right within each size, so the answer is
    stable between runs and reads the way somebody would have written it by hand.
    """
    header, rows = _rows(text, max_rows + 1)
    if not header or not rows:
        return None
    complete = len(rows) <= max_rows
    rows = rows[:max_rows]
    total = len(rows)

    index = {name: position for position, name in enumerate(header)}
    candidates = [
        name
        for name in header
        if not _is_measurement(name, [row[index[name]] for row in rows], vocabulary)
    ]

    def unique(columns: tuple[str, ...]) -> bool:
        seen: set[tuple[str, ...]] = set()
        for row in rows:
            value = tuple(row[index[column]] for column in columns)
            if "" in value:
                # A blank is not an identifier. A key with one is not a key, and treating
                # it as one is how a "unique" column pairs rows that are not the same row.
                return False
            if value in seen:
                return False
            seen.add(value)
        return True

    for column in candidates:
        if unique((column,)):
            return SeedKey(columns=(column,), rows_read=total, complete=complete)

    if MAX_COMBINATION < 2:
        return None
    for i, first in enumerate(candidates):
        for second in candidates[i + 1 :]:
            if unique((first, second)):
                return SeedKey(columns=(first, second), rows_read=total, complete=complete)
    return None

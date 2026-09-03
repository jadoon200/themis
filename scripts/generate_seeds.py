"""Generate the demo project's seed data.

The previous data was too well behaved to test anything. Every row had a match, no key
repeated, and almost nothing sat on a boundary — so seven of eleven generated mutations
moved no numbers at all and the oracle could not judge them.

Realism is not what fixes that; *awkwardness* is. Each property below exists because
some defect class is undetectable without it:

- entities trading in several currencies within one period — otherwise dropping the
  currency from a GROUP BY merges nothing
- revenue entries with no contract — otherwise tightening a LEFT JOIN drops nothing
- entries posted several days after their period — otherwise narrowing an incremental
  lookback loses nothing
- amounts whose cents are not representable in binary floating point, in enough volume
  that a DOUBLE cast actually drifts a total rather than only changing a column type
- a duplicated FX rate row, so a fan-out is worse than the row count alone suggests
- contracts referenced but absent, so an inner join to them loses revenue

Deterministic: same seed, same bytes. The corpus compares runs, so data that moved
between them would make every comparison meaningless.
"""

from __future__ import annotations

import random
import sys
from datetime import date, timedelta
from pathlib import Path

SEED = 20260903
SEEDS_DIR = Path("demo_project/seeds")

ENTITIES = [("SG01", "SGD"), ("UK01", "GBP"), ("US01", "USD"), ("EU01", "EUR")]
# Every entity also books in USD, so a period can hold more than one currency.
SECONDARY = "USD"
PERIODS = [date(2026, m, 1) for m in range(1, 7)]
CURRENCIES = ["USD", "SGD", "GBP", "EUR", "JPY"]


def _accounts() -> str:
    rows = ["account_id,account_code,account_name,account_type,entity_code,is_intercompany"]
    n = 1
    for entity, _ in ENTITIES:
        for code, name, kind in (
            (4000, "Revenue - Software Licences", "revenue"),
            (4010, "Revenue - Support Services", "revenue"),
            (4020, "Revenue - Professional Services", "revenue"),
            (5000, "Cost of Revenue", "expense"),
            (4900, "Intercompany Revenue", "revenue"),
        ):
            inter = "true" if code == 4900 else "false"
            rows.append(f"A{n:03d},{code},{name},{kind},{entity},{inter}")
            n += 1
    return "\n".join(rows) + "\n"


def _fx() -> str:
    """Monthly rates, plus one duplicated row.

    The duplicate matters: a join that already fans out on currency alone fans out
    further, so the measured multiple is not simply the number of months.
    """
    rng = random.Random(SEED + 1)
    rows = ["currency_code,rate_date,rate,rate_source"]
    base = {"USD": 1.0, "SGD": 0.7412, "GBP": 1.2734, "EUR": 1.089, "JPY": 0.0067}
    for currency in CURRENCIES:
        for period in PERIODS:
            rate = base[currency] * (1 + rng.uniform(-0.03, 0.03))
            rows.append(f"{currency},{period},{rate:.8f},ecb")
    # A rate published twice for one currency-month.
    rows.append(f"SGD,{PERIODS[2]},{base['SGD'] * 1.001:.8f},ecb_restated")
    return "\n".join(rows) + "\n"


def _contracts() -> tuple[str, list[str]]:
    rng = random.Random(SEED + 2)
    rows = [
        "contract_id,customer_id,contract_start,contract_end,recognition_method,"
        "term_months,customer_email"
    ]
    ids: list[str] = []
    for i in range(1, 41):
        cid = f"C{i:03d}"
        ids.append(cid)
        start = PERIODS[rng.randrange(len(PERIODS))]
        months = rng.choice([3, 6, 12])
        method = rng.choice(["ratable", "point_in_time"])
        rows.append(
            f"{cid},CUST{rng.randrange(1, 15):02d},{start},"
            f"{start + timedelta(days=30 * months)},{method},{months},"
            f"ops{i}@counterparty{i % 7}.example"
        )
    return "\n".join(rows) + "\n", ids


def _gl(contract_ids: list[str]) -> str:
    rng = random.Random(SEED + 3)
    rows = [
        "entry_id,account_id,posting_date,period_month,currency_code,amount_minor,"
        "entry_type,contract_id,is_reversal"
    ]
    accounts = [f"A{n:03d}" for n in range(1, len(ENTITIES) * 5 + 1)]
    # Contracts referenced by entries but absent from the contracts table.
    orphans = ["C900", "C901"]

    entry = 1
    for period in PERIODS:
        for _ in range(40):
            account = accounts[rng.randrange(len(accounts))]
            entity_index = (int(account[1:]) - 1) // 5
            _, home = ENTITIES[entity_index]
            # A fifth of entries book in USD rather than the entity's home currency,
            # so a period holds more than one.
            currency = SECONDARY if rng.random() < 0.2 else home

            # Most entries post inside their period; some arrive up to five days late,
            # which is what a narrowed lookback window loses.
            offset = rng.randrange(0, 28)
            if rng.random() < 0.15:
                offset = 27 + rng.randrange(1, 6)
            posting = period + timedelta(days=offset)

            # Cents that binary floating point cannot represent exactly. In volume,
            # this is what makes a DOUBLE cast drift a total rather than merely
            # retype a column.
            minor = rng.randrange(1_000, 5_000_000) * 100 + rng.choice([7, 13, 29, 71])

            if rng.random() < 0.08:
                contract = ""  # revenue with no contract at all
            elif rng.random() < 0.04:
                contract = orphans[rng.randrange(len(orphans))]
            else:
                contract = contract_ids[rng.randrange(len(contract_ids))]

            reversal = rng.random() < 0.06
            kind = "debit" if account.endswith(("4", "9")) else "credit"
            amount = -minor if reversal else minor
            rows.append(
                f"E{entry:05d},{account},{posting},{period},{currency},{amount},"
                f"{kind},{contract},{'true' if reversal else 'false'}"
            )
            entry += 1
    return "\n".join(rows) + "\n"


def main() -> int:
    contracts, ids = _contracts()
    (SEEDS_DIR / "raw_accounts.csv").write_text(_accounts())
    (SEEDS_DIR / "raw_fx_rates.csv").write_text(_fx())
    (SEEDS_DIR / "raw_contracts.csv").write_text(contracts)
    (SEEDS_DIR / "raw_gl_entries.csv").write_text(_gl(ids))
    for name in ("raw_accounts", "raw_fx_rates", "raw_contracts", "raw_gl_entries"):
        path = SEEDS_DIR / f"{name}.csv"
        print(f"  {name}: {len(path.read_text().splitlines()) - 1} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())

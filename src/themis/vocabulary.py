"""The words a project uses for the things THEMIS has to recognise by name.

Several checks cannot be made from structure alone. Whether a column holds money,
whether it holds personal data, whether a model feeds something someone signs, and
whether a folder is read outside the team — each is decided by matching names. The
defaults describe the demo project and common convention. A real project has its own
words (`ntnl`, `mtm`, `pnl`, a `regulator` tag, a `published/` folder), and a name this
list does not know is a rule that silently never fires on it.

So the vocabulary is configuration, not code: one place, read from settings, handed to
every stage that matches names. `themis profile` reports how often each list matches a
project, which is how a mismatch shows up before it costs a missed finding.
"""

from __future__ import annotations

from dataclasses import dataclass

# Substrings that mark a column as monetary. Deliberately broad: a false positive costs a
# reviewer one glance, a false negative costs a restatement.
MONEY_HINTS: tuple[str, ...] = (
    "amount",
    "amt",
    "price",
    "cost",
    "revenue",
    "balance",
    "value",
    "total",
    "fee",
    "tax",
    "charge",
    "payment",
    "salary",
    "rate",
    "usd",
    "eur",
    "gbp",
    "sgd",
)

# Column-name hints for personal or restricted data. A false positive costs one glance, a
# false negative puts personal data in a shared mart.
SENSITIVE_HINTS: tuple[str, ...] = (
    "email",
    "phone",
    "address",
    "postcode",
    "zipcode",
    "ssn",
    "nric",
    "passport",
    "dob",
    "birth",
    "salary",
    "national_id",
    "tax_id",
    "account_number",
    "iban",
    "card_number",
    "full_name",
    "first_name",
    "last_name",
)

# Columns naming the currency a row is denominated in. Summing an amount across rows of
# different currencies produces a number with no unit, which is the failure these exist to
# recognise: it looks like money, it ties to nothing, and nothing about it is malformed.
CURRENCY_HINTS: tuple[str, ...] = ("currency", "ccy_code", "curr_code", "iso_currency")

# Amounts that are denominated in whatever currency the row happens to be in. Summing one
# without grouping by the currency mixes units. Names, because dbt projects rarely declare
# types and never declare units — a project that spells it differently sets this.
TRANSACTION_CURRENCY_HINTS: tuple[str, ...] = (
    "txn_ccy",
    "trade_ccy",
    "local_ccy",
    "local_amount",
    "amount_lcy",
    "original_amount",
    "source_amount",
)

# Amounts already converted to one reporting currency, which sum correctly across rows.
# Checked first: `revenue_usd` is monetary and denominated, and must never be reported.
REPORTING_CURRENCY_HINTS: tuple[str, ...] = (
    "_usd",
    "_eur",
    "_gbp",
    "_chf",
    "_jpy",
    "_base_ccy",
    "_reporting_ccy",
    "_rpt_ccy",
)

# Columns naming the accounting period a row belongs to. A change that moves a figure in a
# period that has already been reported is a restatement, whatever else it is.
PERIOD_HINTS: tuple[str, ...] = (
    "period",
    "month",
    "quarter",
    "fiscal",
    "as_of",
    "asof",
    "reporting_date",
    "business_date",
    "cob_date",
    "value_date",
)

# Amounts stored as whole minor units — cents, pence, satoshi — which is how a ledger
# keeps money integral. Dividing one back to major units without casting first truncates
# under Trino's integer division, and the loss is a fraction of a unit on every row.
MINOR_UNIT_HINTS: tuple[str, ...] = ("_minor", "_cents", "_pence", "_sen", "minor_units")

# Tags a project uses to say a model feeds reconciliation or external reporting.
GOVERNED_TAGS: tuple[str, ...] = ("regulatory", "recon", "control")

# Folders whose models are consumed outside the team that owns them.
PUBLISHED_FOLDERS: tuple[str, ...] = ("marts/", "reporting/", "published/", "exposed/")


@dataclass(frozen=True)
class Vocabulary:
    money_hints: tuple[str, ...] = MONEY_HINTS
    sensitive_hints: tuple[str, ...] = SENSITIVE_HINTS
    currency_hints: tuple[str, ...] = CURRENCY_HINTS
    minor_unit_hints: tuple[str, ...] = MINOR_UNIT_HINTS
    period_hints: tuple[str, ...] = PERIOD_HINTS
    transaction_currency_hints: tuple[str, ...] = TRANSACTION_CURRENCY_HINTS
    reporting_currency_hints: tuple[str, ...] = REPORTING_CURRENCY_HINTS
    governed_tags: tuple[str, ...] = GOVERNED_TAGS
    published_folders: tuple[str, ...] = PUBLISHED_FOLDERS

    def is_monetary(self, column: str) -> bool:
        lowered = column.lower()
        return any(hint in lowered for hint in self.money_hints)

    def is_sensitive(self, column: str) -> bool:
        lowered = column.lower()
        return any(hint in lowered for hint in self.sensitive_hints)

    def is_minor_unit_amount(self, column: str) -> bool:
        lowered = column.lower()
        return any(hint in lowered for hint in self.minor_unit_hints)

    def is_period_column(self, column: str) -> bool:
        lowered = column.lower()
        return any(hint in lowered for hint in self.period_hints)

    def is_currency_column(self, column: str) -> bool:
        lowered = column.lower()
        return any(hint in lowered for hint in self.currency_hints)

    def is_transaction_currency_amount(self, column: str) -> bool:
        """An amount denominated in the row's own currency, so summing it mixes units.

        A reporting-currency name wins: `revenue_usd` is monetary and denominated and
        sums perfectly well, and calling it suspect would flag the correct case in every
        model that converts.
        """
        lowered = column.lower()
        if any(hint in lowered for hint in self.reporting_currency_hints):
            return False
        return any(hint in lowered for hint in self.transaction_currency_hints)

    def is_governed(self, tags: tuple[str, ...] | list[str]) -> bool:
        wanted = {tag.lower() for tag in self.governed_tags}
        return bool(wanted & {tag.lower() for tag in tags})

    def is_published(self, file_path: str) -> bool:
        path = file_path.replace("\\", "/")
        return any(folder in path for folder in self.published_folders)


DEFAULT = Vocabulary()


def from_settings(settings: object) -> Vocabulary:
    """The vocabulary a run was configured with, falling back to the defaults."""
    return Vocabulary(
        money_hints=getattr(settings, "money_column_hints", MONEY_HINTS),
        sensitive_hints=getattr(settings, "sensitive_column_hints", SENSITIVE_HINTS),
        governed_tags=getattr(settings, "governed_tags", GOVERNED_TAGS),
        published_folders=getattr(settings, "published_folders", PUBLISHED_FOLDERS),
        currency_hints=getattr(settings, "currency_column_hints", CURRENCY_HINTS),
        period_hints=getattr(settings, "period_column_hints", PERIOD_HINTS),
        minor_unit_hints=getattr(settings, "minor_unit_hints", MINOR_UNIT_HINTS),
        transaction_currency_hints=getattr(
            settings, "transaction_currency_hints", TRANSACTION_CURRENCY_HINTS
        ),
        reporting_currency_hints=getattr(
            settings, "reporting_currency_hints", REPORTING_CURRENCY_HINTS
        ),
    )

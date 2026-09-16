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

# Tags a project uses to say a model feeds reconciliation or external reporting.
GOVERNED_TAGS: tuple[str, ...] = ("regulatory", "recon", "control")

# Folders whose models are consumed outside the team that owns them.
PUBLISHED_FOLDERS: tuple[str, ...] = ("marts/", "reporting/", "published/", "exposed/")


@dataclass(frozen=True)
class Vocabulary:
    money_hints: tuple[str, ...] = MONEY_HINTS
    sensitive_hints: tuple[str, ...] = SENSITIVE_HINTS
    governed_tags: tuple[str, ...] = GOVERNED_TAGS
    published_folders: tuple[str, ...] = PUBLISHED_FOLDERS

    def is_monetary(self, column: str) -> bool:
        lowered = column.lower()
        return any(hint in lowered for hint in self.money_hints)

    def is_sensitive(self, column: str) -> bool:
        lowered = column.lower()
        return any(hint in lowered for hint in self.sensitive_hints)

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
    )

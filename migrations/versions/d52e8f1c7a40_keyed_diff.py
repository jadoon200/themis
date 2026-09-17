"""keep what pairing rows on the derived key found

Revision ID: d52e8f1c7a40
Revises: c81f47a9b2e3
Create Date: 2026-09-16 18:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "d52e8f1c7a40"
down_revision = "c81f47a9b2e3"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("model_delta", schema=None) as batch_op:
        batch_op.add_column(sa.Column("keyed_diff", _JSON, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("model_delta", schema=None) as batch_op:
        batch_op.drop_column("keyed_diff")

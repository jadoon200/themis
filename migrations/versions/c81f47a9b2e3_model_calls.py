"""keep what each model call was shown and what it answered

Revision ID: c81f47a9b2e3
Revises: 9b4e2c7a1d05
Create Date: 2026-09-16 12:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "c81f47a9b2e3"
down_revision = "9b4e2c7a1d05"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "model_call",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("review_run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seat", sa.String(length=64), nullable=False),
        sa.Column("llm_model", sa.String(length=128), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=True),
        sa.Column("rule_id", sa.String(length=32), nullable=True),
        sa.Column("model_name", sa.String(length=255), nullable=True),
        sa.Column("context", sa.Text(), nullable=False),
        sa.Column("system", sa.Text(), nullable=False),
        sa.Column("response", _JSON, nullable=True),
        sa.Column("accepted", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("rejected_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_model_call_seat", "model_call", ["seat"])
    op.create_index("ix_model_call_fingerprint", "model_call", ["fingerprint"])
    op.create_index("ix_model_call_rule_id", "model_call", ["rule_id"])


def downgrade() -> None:
    op.drop_index("ix_model_call_rule_id", table_name="model_call")
    op.drop_index("ix_model_call_fingerprint", table_name="model_call")
    op.drop_index("ix_model_call_seat", table_name="model_call")
    op.drop_table("model_call")

"""who decided what, and enough of a review to answer questions about it later

Revision ID: e7a2b4c9d1f3
Revises: d52e8f1c7a40
Create Date: 2026-09-21 10:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "e7a2b4c9d1f3"
down_revision = "d52e8f1c7a40"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("review_run", schema=None) as batch_op:
        batch_op.add_column(sa.Column("pr_title", sa.String(length=512), nullable=True))
        batch_op.add_column(sa.Column("pr_author", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("reviewed_models", _JSON, nullable=True))

    with op.batch_alter_table("finding", schema=None) as batch_op:
        batch_op.add_column(sa.Column("disposition_by", sa.String(length=255), nullable=True))

    op.create_table(
        "disposition_event",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "finding_id",
            sa.Integer(),
            sa.ForeignKey("finding.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("disposition", sa.String(length=32), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_disposition_event_at", "disposition_event", ["at"])
    op.create_index("ix_disposition_event_finding", "disposition_event", ["finding_id"])

    op.create_table(
        "run_snapshot",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("review_run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "side", name="uq_run_snapshot_side"),
    )


def downgrade() -> None:
    op.drop_table("run_snapshot")
    op.drop_index("ix_disposition_event_finding", table_name="disposition_event")
    op.drop_index("ix_disposition_event_at", table_name="disposition_event")
    op.drop_table("disposition_event")
    with op.batch_alter_table("finding", schema=None) as batch_op:
        batch_op.drop_column("disposition_by")
    with op.batch_alter_table("review_run", schema=None) as batch_op:
        batch_op.drop_column("reviewed_models")
        batch_op.drop_column("pr_author")
        batch_op.drop_column("pr_title")

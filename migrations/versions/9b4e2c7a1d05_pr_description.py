"""store the pull-request description with a queued run

Revision ID: 9b4e2c7a1d05
Revises: f3ad733ed3cd
Create Date: 2026-09-13 20:10:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "9b4e2c7a1d05"
down_revision = "f3ad733ed3cd"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("review_run", schema=None) as batch_op:
        batch_op.add_column(sa.Column("pr_description", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("review_run", schema=None) as batch_op:
        batch_op.drop_column("pr_description")

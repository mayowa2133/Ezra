"""post lifecycle, structured errors, resumable upload state; analytics snapshot fields

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-27
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # plain ADD COLUMN, not a batch rebuild (see 0002)
    op.add_column("posts", sa.Column("error_code", sa.String(length=60), nullable=True))
    op.add_column("posts", sa.Column("platform_state", sa.String(length=30), nullable=True))
    op.add_column("posts", sa.Column("upload_state", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
    op.add_column("posts", sa.Column("warnings", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
    op.add_column("metric_snapshots", sa.Column("watch_minutes", sa.Float(), nullable=True))
    op.add_column("metric_snapshots", sa.Column("avg_view_pct", sa.Float(), nullable=True))
    op.add_column("metric_snapshots", sa.Column("subscribers_lost", sa.Integer(), nullable=True))
    op.add_column("metric_snapshots", sa.Column("window_start", sa.DateTime(timezone=True), nullable=True))
    op.add_column("metric_snapshots", sa.Column("window_end", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("metric_snapshots", schema=None) as b:
        for col in ("window_end", "window_start", "subscribers_lost", "avg_view_pct", "watch_minutes"):
            b.drop_column(col)
    with op.batch_alter_table("posts", schema=None) as b:
        for col in ("warnings", "upload_state", "platform_state", "error_code"):
            b.drop_column(col)

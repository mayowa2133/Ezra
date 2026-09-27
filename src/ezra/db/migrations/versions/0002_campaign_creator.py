"""campaign creator and suggested hashtags

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-27
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # plain ADD COLUMN: SQLite's batch rebuild would drop `campaigns`, which other tables reference
    op.add_column("campaigns", sa.Column("creator", sa.String(length=120), nullable=True))
    op.add_column("campaigns", sa.Column("suggested_hashtags", sa.JSON(), nullable=False,
                                         server_default=sa.text("'[]'")))


def downgrade() -> None:
    with op.batch_alter_table("campaigns", schema=None) as batch_op:
        batch_op.drop_column("suggested_hashtags")
        batch_op.drop_column("creator")

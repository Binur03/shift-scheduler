"""Add Employee.language for bilingual pages and SMS.

Additive and backfilled with the previous behaviour ("en"), so the column is
safe to apply while the old revision is still serving traffic: rows written by
the old code simply take the server default.

Revision ID: c4a1e9b27f30
Revises: d071a9899da1
Create Date: 2026-09-16
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'c4a1e9b27f30'
down_revision = 'd071a9899da1'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('employees', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('language', sa.String(length=5), nullable=False, server_default='en')
        )


def downgrade():
    with op.batch_alter_table('employees', schema=None) as batch_op:
        batch_op.drop_column('language')

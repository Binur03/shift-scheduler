"""Add Employee.portal_token for the one-link weekly schedule.

Additive and nullable: tokens are minted lazily on first use, so the old
revision keeps working against the migrated schema and rollback is a traffic
switch.

Revision ID: e8b3d1c47a52
Revises: c4a1e9b27f30
Create Date: 2026-09-28
"""
import sqlalchemy as sa
from alembic import op

revision = 'e8b3d1c47a52'
down_revision = 'c4a1e9b27f30'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('employees', schema=None) as batch_op:
        batch_op.add_column(sa.Column('portal_token', sa.String(length=64), nullable=True))
        batch_op.create_index(
            batch_op.f('ix_employees_portal_token'), ['portal_token'], unique=True
        )


def downgrade():
    with op.batch_alter_table('employees', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_employees_portal_token'))
        batch_op.drop_column('portal_token')

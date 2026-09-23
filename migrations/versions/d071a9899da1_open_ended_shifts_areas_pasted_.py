"""open ended shifts areas pasted schedules and punch audit

Revision ID: d071a9899da1
Revises: 9b5611f576b6
Create Date: 2026-09-15 11:57:51.953981

Upgrade (additive / relaxing only — safe with existing data):
  * shifts.end_time becomes nullable   — NULL = "until the job is done"
  * shifts.area                        — optional area, e.g. "Parking"
  * inbound_emails.vendor_id nullable  — pasted schedules have no vendor
  * inbound_emails.source              — "email" | "paste" (existing rows: "email")
  * shift_assignments.check_in_actual_at_utc — real tap time behind a snapped check-in
  * shift_assignments.punch_note       — manager's reason for a manual punch

Rollback caution: once shifts with no end time exist, the *previous* app
revision can't render them (it assumes an end time). A traffic-only rollback
is therefore safe only before the first open-ended shift is created.

Downgrade is lossy by necessity: open-ended shifts get end_time = start_time
(a visible placeholder), and pasted-schedule records (no vendor) are deleted
(their shifts are kept; shifts.inbound_email_id is ON DELETE SET NULL).
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd071a9899da1'
down_revision = '9b5611f576b6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('inbound_emails', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source', sa.String(length=10), server_default='email', nullable=False))
        batch_op.alter_column('vendor_id', existing_type=sa.INTEGER(), nullable=True)

    with op.batch_alter_table('shift_assignments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('check_in_actual_at_utc', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('punch_note', sa.String(length=255), nullable=True))

    with op.batch_alter_table('shifts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('area', sa.String(length=80), nullable=True))
        batch_op.alter_column('end_time', existing_type=sa.TIME(), nullable=True)


def downgrade():
    # Make the NOT NULL constraints satisfiable before restoring them.
    op.execute("UPDATE shifts SET end_time = start_time WHERE end_time IS NULL")
    op.execute("UPDATE shifts SET inbound_email_id = NULL WHERE inbound_email_id IN "
               "(SELECT id FROM (SELECT id FROM inbound_emails WHERE vendor_id IS NULL) AS pasted)")
    op.execute("DELETE FROM inbound_emails WHERE vendor_id IS NULL")

    with op.batch_alter_table('shifts', schema=None) as batch_op:
        batch_op.alter_column('end_time', existing_type=sa.TIME(), nullable=False)
        batch_op.drop_column('area')

    with op.batch_alter_table('shift_assignments', schema=None) as batch_op:
        batch_op.drop_column('punch_note')
        batch_op.drop_column('check_in_actual_at_utc')

    with op.batch_alter_table('inbound_emails', schema=None) as batch_op:
        batch_op.alter_column('vendor_id', existing_type=sa.INTEGER(), nullable=False)
        batch_op.drop_column('source')

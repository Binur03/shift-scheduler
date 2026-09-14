"""vendor email ingestion and sms time punches

Revision ID: 2b322677deb7
Revises: 9fa0580e546d
Create Date: 2026-09-14 13:10:56.223269

Deploy safety
-------------
Purely additive: three new tables, nullable columns, and two NOT NULL columns
that carry a server default (``jobs.timezone``, ``shifts.source``) so existing
rows are backfilled by MySQL itself. The currently running revision never
reads or writes these objects, so it keeps working while the new revision
rolls out (and after a traffic rollback). Downgrade removes only what this
migration added.

Foreign keys are named explicitly — autogenerate leaves them unnamed, which
makes ``drop_constraint`` impossible on MySQL during downgrade.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = '2b322677deb7'
down_revision = '9fa0580e546d'
branch_labels = None
depends_on = None

FK_SHIFTS_VENDOR = 'fk_shifts_vendor_id_vendors'
FK_SHIFTS_INBOUND_EMAIL = 'fk_shifts_inbound_email_id_inbound_emails'


def upgrade():
    op.create_table('vendors',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=160), nullable=False),
    sa.Column('inbound_token', sa.String(length=64), nullable=False),
    sa.Column('allowed_sender_domain', sa.String(length=255), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default='1', nullable=False),
    sa.Column('created_at_utc', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('vendors', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_vendors_inbound_token'), ['inbound_token'], unique=True)

    op.create_table('inbound_emails',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('vendor_id', sa.Integer(), nullable=False),
    sa.Column('message_id', sa.String(length=255), nullable=False),
    sa.Column('from_address', sa.String(length=320), nullable=False),
    sa.Column('subject', sa.String(length=998), nullable=False),
    sa.Column('raw_body', sa.Text().with_variant(mysql.MEDIUMTEXT(), 'mysql'), nullable=False),
    sa.Column('dkim_result', sa.String(length=512), nullable=False),
    sa.Column('received_at_utc', sa.DateTime(), nullable=False),
    sa.Column('parse_status', sa.String(length=20), nullable=False),
    sa.Column('shifts_created', sa.Integer(), server_default='0', nullable=False),
    sa.Column('errors', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['vendor_id'], ['vendors.id'], name='fk_inbound_emails_vendor_id_vendors', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('inbound_emails', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_inbound_emails_message_id'), ['message_id'], unique=True)
        batch_op.create_index(batch_op.f('ix_inbound_emails_vendor_id'), ['vendor_id'], unique=False)

    op.create_table('inbound_sms',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('message_sid', sa.String(length=64), nullable=False),
    sa.Column('from_number', sa.String(length=20), nullable=False),
    sa.Column('body', sa.String(length=1600), nullable=False),
    sa.Column('received_at_utc', sa.DateTime(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=True),
    sa.Column('assignment_id', sa.Integer(), nullable=True),
    sa.Column('result', sa.String(length=20), nullable=False),
    sa.Column('reply_body', sa.String(length=480), nullable=True),
    sa.ForeignKeyConstraint(['assignment_id'], ['shift_assignments.id'], name='fk_inbound_sms_assignment_id_shift_assignments', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], name='fk_inbound_sms_employee_id_employees', ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('inbound_sms', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_inbound_sms_employee_id'), ['employee_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_inbound_sms_from_number'), ['from_number'], unique=False)
        batch_op.create_index(batch_op.f('ix_inbound_sms_message_sid'), ['message_sid'], unique=True)

    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('timezone', sa.String(length=64), server_default='America/Denver', nullable=False))

    with op.batch_alter_table('shift_assignments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('check_in_at_utc', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('check_out_at_utc', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('check_in_message_sid', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('check_out_message_sid', sa.String(length=64), nullable=True))
        batch_op.create_index('ix_assignments_employee_punch', ['employee_id', 'check_out_at_utc'], unique=False)

    with op.batch_alter_table('shifts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source', sa.String(length=20), server_default='manual', nullable=False))
        batch_op.add_column(sa.Column('vendor_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('inbound_email_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_shifts_inbound_email_id'), ['inbound_email_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_shifts_vendor_id'), ['vendor_id'], unique=False)
        batch_op.create_foreign_key(FK_SHIFTS_VENDOR, 'vendors', ['vendor_id'], ['id'], ondelete='SET NULL')
        batch_op.create_foreign_key(FK_SHIFTS_INBOUND_EMAIL, 'inbound_emails', ['inbound_email_id'], ['id'], ondelete='SET NULL')


def downgrade():
    # Foreign keys must go before the indexes MySQL uses to back them.
    with op.batch_alter_table('shifts', schema=None) as batch_op:
        batch_op.drop_constraint(FK_SHIFTS_INBOUND_EMAIL, type_='foreignkey')
        batch_op.drop_constraint(FK_SHIFTS_VENDOR, type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_shifts_vendor_id'))
        batch_op.drop_index(batch_op.f('ix_shifts_inbound_email_id'))
        batch_op.drop_column('inbound_email_id')
        batch_op.drop_column('vendor_id')
        batch_op.drop_column('source')

    with op.batch_alter_table('shift_assignments', schema=None) as batch_op:
        batch_op.drop_index('ix_assignments_employee_punch')
        batch_op.drop_column('check_out_message_sid')
        batch_op.drop_column('check_in_message_sid')
        batch_op.drop_column('check_out_at_utc')
        batch_op.drop_column('check_in_at_utc')

    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_column('timezone')

    op.drop_table('inbound_sms')
    op.drop_table('inbound_emails')
    op.drop_table('vendors')

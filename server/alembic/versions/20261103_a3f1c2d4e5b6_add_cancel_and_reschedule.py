"""add cancel and reschedule to approvals and attempts

Revision ID: a3f1c2d4e5b6
Revises: d98292f953ff
"""

from alembic import op
import sqlalchemy as sa

revision = 'a3f1c2d4e5b6'
down_revision = 'd98292f953ff'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('approvals', sa.Column('old_slot', sa.String(length=40), nullable=True))
    op.add_column('approvals', sa.Column('booking_id', sa.String(length=40), nullable=True))
    op.alter_column('approvals', 'hold_id', existing_type=sa.String(length=40), nullable=True)
    # Every attempt so far was a booking. The default exists only to fill those rows.
    op.add_column(
        'booking_attempts',
        sa.Column('action', sa.String(length=16), nullable=False, server_default='book'),
    )
    op.alter_column('booking_attempts', 'action', server_default=None)
    op.alter_column(
        'booking_attempts', 'hold_id', existing_type=sa.String(length=40), nullable=True
    )
    op.create_index(
        'uq_attempts_open_booking',
        'booking_attempts',
        ['booking_id'],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
        sqlite_where=sa.text("status = 'open'"),
    )


def downgrade() -> None:
    op.drop_index(
        'uq_attempts_open_booking',
        table_name='booking_attempts',
        postgresql_where=sa.text("status = 'open'"),
        sqlite_where=sa.text("status = 'open'"),
    )
    op.execute("DELETE FROM booking_attempts WHERE hold_id IS NULL")
    op.execute("DELETE FROM approvals WHERE hold_id IS NULL")
    op.alter_column(
        'booking_attempts', 'hold_id', existing_type=sa.String(length=40), nullable=False
    )
    op.drop_column('booking_attempts', 'action')
    op.alter_column('approvals', 'hold_id', existing_type=sa.String(length=40), nullable=False)
    op.drop_column('approvals', 'booking_id')
    op.drop_column('approvals', 'old_slot')

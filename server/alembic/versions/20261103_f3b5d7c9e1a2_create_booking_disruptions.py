"""create booking_disruptions and index notifications by user

Revision ID: f3b5d7c9e1a2
Revises: e2a4c6b8d0f1
"""

from alembic import op
import sqlalchemy as sa

revision = 'f3b5d7c9e1a2'
down_revision = 'e2a4c6b8d0f1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'booking_disruptions',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('booking_id', sa.String(length=40), nullable=False),
        sa.Column('user_id', sa.String(length=64), nullable=False),
        sa.Column('session_id', sa.String(length=64), nullable=False),
        sa.Column('workflow_id', sa.Uuid(), nullable=False),
        sa.Column('run_id', sa.Uuid(), nullable=True),
        sa.Column('provider_id', sa.String(length=20), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('reason', sa.String(length=32), nullable=False),
        sa.Column('search_failures', sa.Integer(), nullable=False),
        sa.Column('repeats_seen', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_booking_disruptions')),
        sa.UniqueConstraint('booking_id', name=op.f('uq_booking_disruptions_booking_id')),
    )
    op.create_index(
        op.f('ix_booking_disruptions_user_id'), 'booking_disruptions', ['user_id'], unique=False
    )
    op.create_index(
        'ix_disruptions_detected',
        'booking_disruptions',
        ['created_at'],
        unique=False,
        postgresql_where=sa.text("status = 'detected'"),
        sqlite_where=sa.text("status = 'detected'"),
    )
    op.create_index('ix_notifications_user', 'notifications', ['user_id', 'id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_notifications_user', table_name='notifications')
    op.drop_index(
        'ix_disruptions_detected',
        table_name='booking_disruptions',
        postgresql_where=sa.text("status = 'detected'"),
        sqlite_where=sa.text("status = 'detected'"),
    )
    op.drop_index(op.f('ix_booking_disruptions_user_id'), table_name='booking_disruptions')
    op.drop_table('booking_disruptions')

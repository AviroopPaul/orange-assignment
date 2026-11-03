"""create memories, turn_view, context_log and session_rotations

Revision ID: b7e2d9c4a1f0
Revises: a3f1c2d4e5b6
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'b7e2d9c4a1f0'
down_revision = 'a3f1c2d4e5b6'
branch_labels = None
depends_on = None

_TRUSTED = "status = 'active' AND (source = 'explicit' OR confirmed_at IS NOT NULL)"
_BIG_ID = sa.BigInteger().with_variant(sa.Integer(), 'sqlite')
_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    op.create_table(
        'memories',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.String(length=64), nullable=False),
        sa.Column('key', sa.String(length=40), nullable=False),
        sa.Column('value', sa.Text(), nullable=False),
        sa.Column('scope', sa.String(length=40), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('quote', sa.Text(), nullable=True),
        sa.Column('session_id', sa.String(length=64), nullable=True),
        sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('superseded_by', sa.Uuid(), nullable=True),
        sa.Column('scrub_pending', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_memories')),
    )
    op.create_index('ix_memories_user_status', 'memories', ['user_id', 'status'])
    op.create_index(
        'uq_memories_trusted_key',
        'memories',
        ['user_id', 'key', 'scope'],
        unique=True,
        postgresql_where=sa.text(_TRUSTED),
        sqlite_where=sa.text(_TRUSTED),
    )
    op.create_table(
        'turn_view',
        sa.Column('id', _BIG_ID, autoincrement=True, nullable=False),
        sa.Column('user_id', sa.String(length=64), nullable=False),
        sa.Column('session_id', sa.String(length=64), nullable=False),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_turn_view')),
    )
    op.create_index(
        'ix_turn_view_user_session', 'turn_view', ['user_id', 'session_id', 'id']
    )
    op.create_table(
        'context_log',
        sa.Column('id', _BIG_ID, autoincrement=True, nullable=False),
        sa.Column('run_id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.String(length=64), nullable=False),
        sa.Column('session_id', sa.String(length=64), nullable=False),
        sa.Column('payload', _JSON, nullable=False),
        sa.Column('tokens', _JSON, nullable=False),
        sa.Column('estimated_tokens', sa.Integer(), nullable=False),
        sa.Column('budget', sa.Integer(), nullable=False),
        sa.Column('actual_input_tokens', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['run_id'], ['runs.id'], name=op.f('fk_context_log_run_id_runs'), ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_context_log')),
    )
    op.create_index('ix_context_log_user', 'context_log', ['user_id', 'id'])
    op.create_index(op.f('ix_context_log_run_id'), 'context_log', ['run_id'])
    op.create_table(
        'session_rotations',
        sa.Column('user_id', sa.String(length=64), nullable=False),
        sa.Column('session_id', sa.String(length=64), nullable=False),
        sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('user_id', 'session_id', name=op.f('pk_session_rotations')),
    )


def downgrade() -> None:
    op.drop_table('session_rotations')
    op.drop_index(op.f('ix_context_log_run_id'), table_name='context_log')
    op.drop_index('ix_context_log_user', table_name='context_log')
    op.drop_table('context_log')
    op.drop_index('ix_turn_view_user_session', table_name='turn_view')
    op.drop_table('turn_view')
    op.drop_index(
        'uq_memories_trusted_key',
        table_name='memories',
        postgresql_where=sa.text(_TRUSTED),
        sqlite_where=sa.text(_TRUSTED),
    )
    op.drop_index('ix_memories_user_status', table_name='memories')
    op.drop_table('memories')

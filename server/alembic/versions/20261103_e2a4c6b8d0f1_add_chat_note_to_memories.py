"""add chat_note to memories

Revision ID: e2a4c6b8d0f1
Revises: b7e2d9c4a1f0
"""

from alembic import op
import sqlalchemy as sa

revision = 'e2a4c6b8d0f1'
down_revision = 'b7e2d9c4a1f0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('memories', sa.Column('chat_note', sa.String(length=8), nullable=True))


def downgrade() -> None:
    op.drop_column('memories', 'chat_note')

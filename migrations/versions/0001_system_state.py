"""system_state: heartbeat Worker (2.3.1)

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE system_state (
            key                TEXT PRIMARY KEY,
            value              TEXT NOT NULL,
            updated_at         TIMESTAMPTZ NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE system_state")

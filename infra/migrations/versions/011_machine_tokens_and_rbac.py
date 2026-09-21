"""machine tokens and rbac

Revision ID: 011
Revises: 010
Create Date: 2026-09-19 15:30:00.000000

"""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '011'
down_revision: str | None = '010'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Add role column to operator_users
    op.add_column('operator_users', sa.Column('role', sa.String(32), nullable=False, server_default='operator'))

    # Recreate the view v_operator_auth to include the role
    op.execute("DROP VIEW IF EXISTS v_operator_auth;")
    op.execute("""
    CREATE VIEW v_operator_auth AS
    SELECT id, username, password_hash, totp_secret_ciphertext, totp_secret_nonce, totp_secret_auth_tag, is_active, role
    FROM operator_users;
    """)
    op.execute("GRANT SELECT ON v_operator_auth TO cryptonite_api;")

    # Grant select on role to cryptonite_api
    op.execute("GRANT SELECT (role), UPDATE (role) ON TABLE operator_users TO cryptonite_api;")

    # 2. Create machine_api_tokens table
    op.create_table(
        'machine_api_tokens',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('token_hash', sa.String(64), nullable=False, unique=True),
        sa.Column('name', sa.String(128), nullable=False),
        sa.Column('scopes', postgresql.ARRAY(sa.String(64)), nullable=False),
        sa.Column('created_by', postgresql.UUID(as_uuid=True), sa.ForeignKey('operator_users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )

    op.execute("""
    DO $$
    BEGIN
        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
            GRANT SELECT, INSERT, UPDATE ON TABLE machine_api_tokens TO cryptonite_api;
            GRANT USAGE, SELECT ON SEQUENCE machine_api_tokens_id_seq TO cryptonite_api;
        END IF;
    END $$;
    """)

def downgrade() -> None:
    op.drop_table('machine_api_tokens')
    
    op.execute("DROP VIEW IF EXISTS v_operator_auth;")
    op.execute("""
    CREATE VIEW v_operator_auth AS
    SELECT id, username, password_hash, totp_secret_ciphertext, totp_secret_nonce, totp_secret_auth_tag, is_active
    FROM operator_users;
    """)
    op.execute("GRANT SELECT ON v_operator_auth TO cryptonite_api;")
    
    op.drop_column('operator_users', 'role')


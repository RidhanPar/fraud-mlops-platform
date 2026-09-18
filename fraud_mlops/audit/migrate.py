"""One shot schema job, run as the database admin before the API starts.

- Creates the tables.
- Makes the audit tables append only with triggers that reject UPDATE,
  DELETE and TRUNCATE, even from the table owner.
- Creates the application role with INSERT and SELECT only. The API, the
  monitor and the auditor all connect as that role, so a compromised service
  cannot rewrite history.

    DATABASE_ADMIN_URL=... APP_DB_PASSWORD=... python -m fraud_mlops.audit.migrate
"""

from __future__ import annotations

import os
import re

from sqlalchemy import text

from fraud_mlops.api.store import APPEND_ONLY_TABLES, PredictionStore

APP_ROLE = "fraud_app"


def migrate(admin_url: str, app_password: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_]{8,64}", app_password):
        raise ValueError("APP_DB_PASSWORD must be 8 to 64 letters, digits or underscores")
    store = PredictionStore(admin_url)
    store.create_schema()
    if store.engine.dialect.name != "postgresql":
        return

    with store.engine.begin() as conn:
        conn.execute(text("""
            CREATE OR REPLACE FUNCTION audit_forbid_change() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'audit table % is append only', TG_TABLE_NAME;
            END $$;
        """))
        for t in APPEND_ONLY_TABLES:
            conn.execute(text(f"DROP TRIGGER IF EXISTS {t}_append_only ON {t}"))
            conn.execute(text(f"DROP TRIGGER IF EXISTS {t}_no_truncate ON {t}"))
            conn.execute(text(
                f"CREATE TRIGGER {t}_append_only BEFORE UPDATE OR DELETE ON {t} "
                f"FOR EACH ROW EXECUTE FUNCTION audit_forbid_change()"))
            conn.execute(text(
                f"CREATE TRIGGER {t}_no_truncate BEFORE TRUNCATE ON {t} "
                f"FOR EACH STATEMENT EXECUTE FUNCTION audit_forbid_change()"))

        exists = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": APP_ROLE}).first()
        verb = "ALTER" if exists else "CREATE"
        # Password is validated above to a safe character set; roles cannot take bind parameters.
        conn.execute(text(f"{verb} ROLE {APP_ROLE} LOGIN PASSWORD '{app_password}'"))
        db = conn.execute(text("SELECT current_database()")).scalar_one()
        conn.execute(text(f'GRANT CONNECT ON DATABASE "{db}" TO {APP_ROLE}'))
        conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}"))
        conn.execute(text(f"REVOKE ALL ON {', '.join(APPEND_ONLY_TABLES)} FROM {APP_ROLE}"))
        conn.execute(text(f"GRANT SELECT, INSERT ON {', '.join(APPEND_ONLY_TABLES)} TO {APP_ROLE}"))
        conn.execute(text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}"))


def main() -> None:
    migrate(os.environ["DATABASE_ADMIN_URL"], os.environ["APP_DB_PASSWORD"])
    print("schema ready: append only triggers installed, app role has INSERT and SELECT only")


if __name__ == "__main__":
    main()

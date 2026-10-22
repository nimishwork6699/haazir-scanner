"""Apply supabase/migrations/*.sql in order. Needs SUPABASE_DB_URL (Supabase → Connect → Session pooler).

    python -m haazir.migrate
"""

import os
import sys
from pathlib import Path

import psycopg

from . import config  # noqa: F401  (loads .env)

MIGRATIONS = Path(__file__).resolve().parents[2] / "supabase" / "migrations"


def main() -> int:
    url = os.environ.get("SUPABASE_DB_URL", "").strip()
    if not url:
        print("SUPABASE_DB_URL is not set in .env")
        return 1
    with psycopg.connect(url, autocommit=True) as conn:
        for f in sorted(MIGRATIONS.glob("*.sql")):
            conn.execute(f.read_text(encoding="utf-8"))
            print(f"applied {f.name}")
        conn.execute("notify pgrst, 'reload schema'")
    return 0


if __name__ == "__main__":
    sys.exit(main())

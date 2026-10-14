"""Drop and recreate the database named in DATABASE_URL (used by the e2e runner).

Usage: DATABASE_URL=postgresql+psycopg://user:pass@host:5432/swell_e2e python scripts/reset_database.py
Refuses to touch a database whose name does not end in _e2e or _test, optionally followed by
one scratch suffix (swell_e2e_ci2, swell_test_alice).
"""

from __future__ import annotations

import os
import re
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

url = make_url(os.environ["DATABASE_URL"])
name = url.database or ""
if not re.search(r"_(e2e|test)(_[A-Za-z0-9]+)?$", name):
    sys.exit(
        f"refusing to reset database {name!r} (must end in _e2e or _test, optionally "
        "followed by one _<suffix>)"
    )
admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
with admin.connect() as conn:
    conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    conn.execute(text(f'CREATE DATABASE "{name}"'))
print(f"database {name} recreated")

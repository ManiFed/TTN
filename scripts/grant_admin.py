#!/usr/bin/env python3
"""
Grant (or revoke) the 'admin' role on a cloud user account by email.

Admin role gates admin-only actions tied to a signed-in account rather than
the shared X-Admin-Key ops secret — currently just dry-run testing mode
(PUT/DELETE /api/v1/admin/nodes/<node_id>/dry-run, see cloud/auth.py::
require_admin_member).

Usage:
    DATABASE_URL=postgres://... python3 scripts/grant_admin.py user@example.com
    DATABASE_URL=postgres://... python3 scripts/grant_admin.py user@example.com --revoke
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cloud import db


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        print(__doc__)
        sys.exit(1)
    email = args[0].strip().lower()
    role = "member" if "--revoke" in sys.argv else "admin"

    db.init()
    user = db.query_one("SELECT user_id, role FROM users WHERE email = %s", (email,))
    if user is None:
        print(f"No user found with email {email!r}")
        sys.exit(1)
    db.execute("UPDATE users SET role = %s WHERE email = %s", (role, email))
    print(f"{email}: role {user['role']!r} -> {role!r}")


if __name__ == "__main__":
    main()

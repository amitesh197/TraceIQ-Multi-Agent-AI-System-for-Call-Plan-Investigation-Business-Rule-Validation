"""Set or change a user's password in config/users.yaml.

  python scripts/set_password.py cor_analyst
"""
import getpass
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from traceiq.access import USERS_FILE, hash_password  # noqa: E402

if len(sys.argv) != 2:
    sys.exit("Usage: python scripts/set_password.py <user_id>")
uid = sys.argv[1]
with open(USERS_FILE) as f:
    text = f.read()
users = yaml.safe_load(text)["users"]
if uid not in users:
    sys.exit(f"Unknown user {uid}. Users: {', '.join(users)}")
p1 = getpass.getpass("New password: ")
if len(p1) < 8 or p1 != getpass.getpass("Repeat password: "):
    sys.exit("Passwords must match and be at least 8 characters.")
old = users[uid].get("password_hash", "")
new = hash_password(p1)
text = text.replace(old, new) if old else text.replace(f"  {uid}:\n", f"  {uid}:\n    password_hash: \"{new}\"\n", 1)
with open(USERS_FILE, "w") as f:
    f.write(text)
print(f"Password updated for {uid}.")

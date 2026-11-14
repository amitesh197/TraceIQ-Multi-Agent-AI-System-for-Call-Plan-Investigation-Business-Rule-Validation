"""Role and territory access, enforced in code (the Access agent and every tool use this)."""
import datetime as dt
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass, field

import yaml

from .db import ROOT

USERS_FILE = os.path.join(ROOT, "config", "users.yaml")


class AccessDenied(Exception):
    pass


@dataclass
class AccessContext:
    user_id: str
    name: str
    role: str
    teams: set = field(default_factory=set)   # empty set + all_teams=True means everything
    all_teams: bool = False
    expires: str | None = None

    def is_expired(self, today: dt.date | None = None) -> bool:
        if not self.expires:
            return False
        return (today or dt.date.today()) > dt.date.fromisoformat(self.expires)

    def can_see(self, territory_or_team: str) -> bool:
        if self.all_teams:
            return True
        team = (territory_or_team or "")[:6]
        return team in self.teams

    def require(self, territory_or_team: str):
        if not self.can_see(territory_or_team):
            raise AccessDenied(f"{self.name} does not have access to {territory_or_team[:6]}")


def load_user(user_id: str) -> AccessContext:
    with open(USERS_FILE) as f:
        users = yaml.safe_load(f)["users"]
    if user_id not in users:
        raise AccessDenied(f"unknown user {user_id}")
    u = users[user_id]
    teams = u.get("teams", [])
    return AccessContext(user_id=user_id, name=u["name"], role=u["role"],
                         teams=set() if teams == "*" else set(teams), all_teams=teams == "*",
                         expires=u.get("access_expires"))


# ---------------------------------------------------------------- sign-in (demo passwords; replace with SSO in production)
def hash_password(password: str, iterations: int = 200_000) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), iterations).hex()
    return f"pbkdf2${iterations}${salt}${h}"


def verify_password(user_id: str, password: str) -> bool:
    with open(USERS_FILE) as f:
        users = yaml.safe_load(f)["users"]
    stored = users.get(user_id, {}).get("password_hash", "")
    try:
        _, it, salt, h = stored.split("$")
    except ValueError:
        return False
    test = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(it)).hex()
    return hmac.compare_digest(test, h)


def list_users() -> dict:
    with open(USERS_FILE) as f:
        return yaml.safe_load(f)["users"]

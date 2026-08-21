"""
Record-level access control for presentations.

Mirrors the `access_dict` model documented in
ambivo_api/docs/roles-and-permissions.md so that team-based sharing
semantics are consistent across products.

`access_dict` shape, stored on each presentation document:

    {
      "<userid>": ["C", "R", "U", "D"],   # CRUD privileges for one user
      "<userid>": "*",                    # equivalent to full CRUD
      "all":      "*",                    # every tenant user has full CRUD
      "*":        "U"  | ["U", "R"]       # every tenant user has the listed privs
    }

The `A` (admin) code from `module_access_dict` is not valid at record
level and is dropped during any team conversion.

Authorization for a privilege check is granted when ANY of these hold:
  1. The caller is a tenant admin (JWT `is_tenant_admin`).
  2. The caller is the record owner (`doc["userid"] == caller_userid`).
  3. `access_dict["all"] == "*"` (wildcard for tenant).
  4. `access_dict["*"]` includes the requested privilege.
  5. `access_dict[caller_userid]` includes the requested privilege.
  6. `access_dict[caller_userid] == "*"` (full CRUD for this user).
"""
from __future__ import annotations

from typing import Iterable

Privilege = str  # "C" | "R" | "U" | "D"

_VALID_PRIVS = {"C", "R", "U", "D"}


def _entry_grants(entry, priv: Privilege) -> bool:
    """Does an access_dict value grant `priv`?

    Accepts the wildcard `"*"` (means all CRUD) or a list/iterable of
    privilege codes. Unknown shapes return False.
    """
    if entry == "*":
        return priv in _VALID_PRIVS
    if isinstance(entry, str):
        return entry.upper() == priv
    if isinstance(entry, (list, tuple, set)):
        return any(isinstance(p, str) and p.upper() == priv for p in entry)
    return False


def can_access(user: dict, doc: dict, priv: Privilege) -> bool:
    """Return True iff `user` may perform `priv` on the presentation `doc`.

    Caller is expected to have already checked tenant scope (i.e. doc
    belongs to the same tenant). This function does not re-check
    tenant_id — it assumes the caller fetched the doc via a tenant-
    scoped query.
    """
    if priv not in _VALID_PRIVS:
        return False

    if user.get("is_tenant_admin"):
        return True

    caller_id = user.get("userid")
    if not caller_id:
        return False

    owner_id = doc.get("userid") or doc.get("user_id")
    if owner_id and caller_id == owner_id:
        return True

    access_dict = doc.get("access_dict") or {}
    if not isinstance(access_dict, dict):
        return False

    # `all: "*"` is the tenant-wide wildcard. Anything else under "all"
    # is not honored — match the existing reader semantics in ambivo_api.
    if access_dict.get("all") == "*":
        return True

    # `*` key grants a privilege to every tenant user. Value may be "*"
    # (all CRUD), a single priv string, or a list.
    star_entry = access_dict.get("*")
    if star_entry is not None and _entry_grants(star_entry, priv):
        return True

    # Per-userid grant.
    user_entry = access_dict.get(caller_id)
    if user_entry is not None and _entry_grants(user_entry, priv):
        return True

    return False


def default_access_dict_for_creator(creator_userid: str) -> dict:
    """Backwards-compatible default for newly created presentations.

    Today every tenant member has full access to every presentation.
    To preserve that behavior we seed `{"all": "*"}` rather than locking
    to the creator — users can later edit the dict to restrict.
    """
    # creator_userid is accepted for symmetry / future tightening but
    # intentionally unused: the default is wide open.
    del creator_userid
    return {"all": "*"}


def ensure_access_dict(doc: dict) -> dict:
    """Return an access_dict for a doc, defaulting to `{"all": "*"}` for
    legacy records that predate this field. Does NOT persist the
    default — callers can write it back if they want to migrate lazily.
    """
    existing = doc.get("access_dict")
    if isinstance(existing, dict) and existing:
        return existing
    return {"all": "*"}


def merge_access_dict(base: dict, additions: dict) -> dict:
    """Per-userid merge: keys in `additions` overwrite keys in `base`.

    Matches `apply_team` merge semantics — no per-privilege union, the
    incoming entry for a userid replaces the existing one wholesale.
    """
    out = dict(base or {})
    for k, v in (additions or {}).items():
        if v is None:
            continue
        out[k] = v
    return out


def filter_userids(access_dict: dict, remove_userids: Iterable[str]) -> dict:
    """Drop the given userids from an access_dict. Wildcards are
    preserved unless they're in `remove_userids` explicitly.
    """
    remove = set(remove_userids or [])
    return {k: v for k, v in (access_dict or {}).items() if k not in remove}

"""
Read-only access to the shared `team` collection owned by ambivo_api.

The collection lives in the same MongoDB; we never write to it from
content-portal. Use ambivo_api's /securityqa/team endpoints to manage
teams (CRUD).

The conversion semantics here mirror `Team.to_access_dict()` in
ambivo_api/src/omnilonely/data/security_qa.py:33797 so that applying
a team to a presentation produces the same record-level grants you'd
get applying it to a lead/opportunity/order/invoice.
"""
from __future__ import annotations

import logging
from typing import Iterable

from bson import ObjectId
from bson.errors import InvalidId

from app.db import get_db

logger = logging.getLogger(__name__)

COLLECTION = "team"

_RECORD_CODES = {"C", "R", "U", "D"}  # `A`/admin is team-scoped, never record-level


def _normalize_member_privs(record_access_list) -> list[str]:
    """Convert a team member's record_access_list to access_dict codes.

    Lowercase letters are uppercased, `A` is dropped, unknown codes are
    dropped, and the result is de-duplicated while preserving the
    canonical CRUD order (so two members with the same effective access
    serialize identically).
    """
    if not isinstance(record_access_list, (list, tuple, set)):
        return []
    seen: set[str] = set()
    for code in record_access_list:
        if not isinstance(code, str):
            continue
        u = code.upper()
        if u in _RECORD_CODES:
            seen.add(u)
    # Canonical order: C, R, U, D — matches how ambivo_api emits them.
    return [c for c in ("C", "R", "U", "D") if c in seen]


def team_to_access_dict(team_doc: dict) -> dict:
    """Convert a team document's userid_dict_list to an access_dict.

    Mirrors `Team.to_access_dict()` rules:
      - lowercase codes upper-cased
      - `a` dropped (not valid at record level)
      - dedup, canonical order
      - members with missing/empty privs after normalization → skipped
      - members with missing/blank userid → skipped
      - empty userid_dict_list → empty dict (caller logs at INFO)
    """
    out: dict[str, list[str]] = {}
    for member in (team_doc.get("userid_dict_list") or []):
        if not isinstance(member, dict):
            continue
        uid = member.get("userid")
        if not uid or not isinstance(uid, str):
            continue
        privs = _normalize_member_privs(member.get("record_access_list"))
        if not privs:
            continue
        out[uid] = privs
    return out


async def get_team(team_id: str, tenant_id: str) -> dict | None:
    """Fetch a team by id, scoped to the caller's tenant. Returns the
    raw document or None if not found or wrong tenant.

    Cross-tenant lookups are silently treated as "not found" — match
    ambivo_api's ShareService._get_team behavior.
    """
    try:
        oid = ObjectId(team_id)
    except (InvalidId, TypeError):
        return None
    coll = get_db()[COLLECTION]
    return await coll.find_one({"_id": oid, "tenant_id": tenant_id})


async def list_teams(tenant_id: str) -> list[dict]:
    """Return all active teams for a tenant, lightest projection. Used
    by the share UI's team picker — sorted by name for stable display.
    """
    coll = get_db()[COLLECTION]
    cursor = coll.find(
        {"tenant_id": tenant_id, "$or": [{"onhold": {"$ne": True}}, {"onhold": {"$exists": False}}]},
        {
            "name": 1,
            "description": 1,
            "userid_dict_list": 1,
            "tags": 1,
        },
    ).sort("name", 1)
    return [d async for d in cursor]


def is_team_member(team_doc: dict, userid: str) -> bool:
    """True if `userid` appears in the team's userid_dict_list."""
    if not userid:
        return False
    for member in (team_doc.get("userid_dict_list") or []):
        if isinstance(member, dict) and member.get("userid") == userid:
            return True
    return False


def team_summary(team_doc: dict) -> dict:
    """Light wire shape for the team picker — id, name, member count,
    description, tags. Strips the full member list to keep payloads
    bounded.
    """
    return {
        "team_id": str(team_doc["_id"]),
        "name": team_doc.get("name") or "",
        "description": team_doc.get("description") or "",
        "tags": team_doc.get("tags") or [],
        "member_count": len(team_doc.get("userid_dict_list") or []),
    }

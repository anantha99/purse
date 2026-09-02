"""REST skills smoke path against a real database (C5.3, PRD §10).

Mirrors ``test_rest_crud.py``'s memory round trip for the skills endpoints:
``PUT /v1/skills/{name}`` → ``GET /v1/skills/{name}`` → ``GET /v1/skills``, plus
the error paths a client actually hits — a missing skill, a missing
``skills:write`` scope, an oversized document, and a frontmatter name that
disagrees with the path.

``db``-marked, so it skips without a database (``tests/conftest.py``) and runs
for real in CI where ``REQUIRE_DB=1`` turns a skip into a failure.
"""

from __future__ import annotations

import pytest

from purse.skills.parse import MAX_CONTENT_BYTES
from tests.gateway.conftest import GOOD_TOKEN, DbGateway

pytestmark = pytest.mark.db

AUTH = {"Authorization": f"Bearer {GOOD_TOKEN}"}

#: The scopes the skills endpoints need. ``db_gateway``'s seeded connection only
#: carries ``memory:*`` scopes, but ``build(granted=...)`` swaps in a fake
#: ``require_scope`` that checks this set directly — the same mechanism the
#: memory scope-denial tests use, see ``tests/gateway/test_rest_shape.py``.
SKILLS_SCOPES = {"skills:read", "skills:write"}


def _doc(name: str, version: str = "1.0.0", *, body: str = "Body text.") -> str:
    return f"---\nname: {name}\ndescription: A {name} skill.\nversion: {version}\n---\n{body}\n"


# ---------------------------------------------------------------------------
# The round trip
# ---------------------------------------------------------------------------


def test_put_then_get_then_list_round_trips(db_gateway: DbGateway) -> None:
    with db_gateway.client(granted=SKILLS_SCOPES) as client:
        put = client.put("/v1/skills/deploy", headers=AUTH, json={"content": _doc("deploy")})
        assert put.status_code == 200
        assert put.json() == {"name": "deploy", "version": "1.0.0"}

        got = client.get("/v1/skills/deploy", headers=AUTH)
        assert got.status_code == 200
        record = got.json()
        assert record["name"] == "deploy"
        assert record["version"] == "1.0.0"
        assert record["description"] == "A deploy skill."
        assert record["body"].strip() == "Body text."
        assert record["frontmatter"]["name"] == "deploy"

        listed = client.get("/v1/skills", headers=AUTH)
        assert listed.status_code == 200
        assert listed.json() == {
            "skills": [{"name": "deploy", "description": "A deploy skill.", "version": "1.0.0"}]
        }


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_get_missing_skill_is_404(db_gateway: DbGateway) -> None:
    with db_gateway.client(granted=SKILLS_SCOPES) as client:
        response = client.get("/v1/skills/does-not-exist", headers=AUTH)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"


def test_put_without_write_scope_is_403(db_gateway: DbGateway) -> None:
    """``skills:read`` alone must not be enough to write (PRD §10)."""
    with db_gateway.client(granted={"skills:read"}) as client:
        response = client.put("/v1/skills/deploy", headers=AUTH, json={"content": _doc("deploy")})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "UNAUTHORIZED_SCOPE"

        # And nothing was written: the scope check runs before the service.
        with db_gateway.client(granted=SKILLS_SCOPES) as reader:
            assert reader.get("/v1/skills", headers=AUTH).json()["skills"] == []


def test_oversized_skill_document_is_413(db_gateway: DbGateway) -> None:
    header = "---\nname: big\ndescription: d\nversion: 1.0.0\n---\n"
    body = "a" * (MAX_CONTENT_BYTES + 1)
    with db_gateway.client(granted=SKILLS_SCOPES) as client:
        response = client.put("/v1/skills/big", headers=AUTH, json={"content": header + body})
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_frontmatter_name_mismatch_is_422(db_gateway: DbGateway) -> None:
    with db_gateway.client(granted=SKILLS_SCOPES) as client:
        response = client.put("/v1/skills/deploy", headers=AUTH, json={"content": _doc("release")})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION"

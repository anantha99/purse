"""Parse and validate a skill document: YAML frontmatter + markdown body (C5.1).

A skill is one markdown file that opens with a YAML frontmatter block fenced by
``---`` lines::

    ---
    name: purse-save-policy
    description: What is worth saving to memory.
    version: 1.0.0
    updated_at: 2026-09-01T00:00:00Z
    ---
    # the body markdown starts here

The frontmatter carries four fields (``updated_at`` optional); everything after
the closing fence is the body, preserved verbatim. This module turns that text
into a validated :class:`ParsedSkill`, or raises a skills
:class:`~purse.skills.errors.SkillError` whose ``code`` is already the wire code
the gateway returns.

.. rubric:: What "valid" means

* **Size** — the whole document is ≤ :data:`MAX_CONTENT_BYTES` UTF-8 bytes
  (PRD §8.3's 64 KB inline cap). Measured in bytes, not characters, because the
  cap is a storage claim. Over the cap is ``PAYLOAD_TOO_LARGE``.
* **name** — non-empty and kebab-case (:data:`NAME_PATTERN`): lowercase
  alphanumerics and hyphens, not starting with a hyphen. It becomes part of a
  tool call (``get_skill("purse-save-policy")``) and a natural key, so it is kept
  to a small, unsurprising alphabet.
* **version** — strict ``MAJOR.MINOR.PATCH`` (:data:`VERSION_PATTERN`), each part
  digits. Pre-release / build metadata are deliberately not accepted in the MVP;
  ``(workspace, name, version)`` is a unique key and a bare semver triple keeps it
  legible.
* **description** — a non-empty string.
* **updated_at** — optional ISO 8601. A YAML timestamp (parsed to a
  ``datetime``/``date``) is accepted and normalised to an ISO string; a plain
  string is validated with :meth:`datetime.datetime.fromisoformat` (accepting a
  trailing ``Z``).

.. rubric:: content_hash

The content address is the sha256 of the whole document, computed through
:func:`purse.db.repo.content_hash` — the *same* function ``Repo.put_skill`` uses
to fill the ``content_hash`` column, so an upsert can compare a candidate's hash
against a stored version without re-deriving it a second, possibly divergent, way.

.. rubric:: Frontmatter is stored as JSONB

The parsed frontmatter lands in ``skills.frontmatter`` (a ``jsonb`` column), so it
must be JSON-serialisable. YAML happily produces ``datetime``/``date`` objects,
which ``json.dumps`` cannot encode — so the frontmatter is passed through
:func:`_json_safe` (datetimes → ISO strings, keys → strings) and then proven
serialisable before it is handed back. A document whose frontmatter cannot be
represented as JSON is a ``VALIDATION`` error, not a 500 at flush time.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from purse.db.repo import content_hash
from purse.skills.errors import PayloadTooLargeError, ValidationError

__all__ = [
    "MAX_CONTENT_BYTES",
    "NAME_PATTERN",
    "VERSION_PATTERN",
    "ParsedSkill",
    "extract_body",
    "parse_skill",
]

#: PRD §8.3: skills are stored inline, capped at 64 KB. Measured in UTF-8 bytes.
MAX_CONTENT_BYTES = 64 * 1024

#: Kebab-case: lowercase alphanumerics and hyphens, not leading with a hyphen.
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")

#: Strict ``MAJOR.MINOR.PATCH`` — each component one or more digits.
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")

_FENCE = "---"


@dataclass(frozen=True, slots=True)
class ParsedSkill:
    """A validated skill document, split into its parts.

    ``content`` is the original document verbatim (what gets stored and hashed);
    ``frontmatter`` is the JSON-safe parsed block; ``body`` is everything after
    the closing fence.
    """

    name: str
    description: str
    version: str
    body: str
    content: str
    content_hash: str
    updated_at: str | None = None
    frontmatter: dict[str, Any] = field(default_factory=dict)


def parse_skill(content: str) -> ParsedSkill:
    """Parse and validate *content*, or raise a skills ``SkillError``.

    The size check runs first (before any YAML work), so a hostile 10 MB document
    is rejected without being parsed.
    """
    if not isinstance(content, str):  # pragma: no cover - typed, but callers cross a wire
        raise ValidationError("skill content must be a string")

    size = len(content.encode("utf-8"))
    if size > MAX_CONTENT_BYTES:
        raise PayloadTooLargeError(
            f"skill is {size} bytes; the limit is {MAX_CONTENT_BYTES} bytes of UTF-8"
        )

    frontmatter_text, body = _split_frontmatter(content)
    data = _load_frontmatter(frontmatter_text)

    name = _require_str(data, "name")
    description = _require_str(data, "description")
    version = _require_str(data, "version")
    _validate_name(name)
    _validate_version(version)
    updated_at = _normalise_updated_at(data.get("updated_at"))

    safe_frontmatter = _json_safe(data)
    if updated_at is not None:
        safe_frontmatter["updated_at"] = updated_at
    _require_json_serialisable(safe_frontmatter)

    return ParsedSkill(
        name=name,
        description=description,
        version=version,
        updated_at=updated_at,
        body=body,
        content=content,
        content_hash=content_hash(content),
        frontmatter=safe_frontmatter,
    )


def extract_body(content: str) -> str:
    """The markdown body of a stored document, best-effort.

    Used to rebuild a :class:`~purse.skills.records.SkillRecord` from a stored
    row, where the document already parsed cleanly on the way in. If a document
    somehow has no frontmatter fence, the whole thing is treated as the body
    rather than raising — a read of stored data should not fail because of how it
    was once written.
    """
    try:
        _, body = _split_frontmatter(content)
    except ValidationError:
        return content
    return body


# ---------------------------------------------------------------------------
# Frontmatter splitting and loading
# ---------------------------------------------------------------------------


def _split_frontmatter(content: str) -> tuple[str, str]:
    """Return ``(frontmatter_text, body)``; raise if the fenced block is absent.

    The document must open with a ``---`` fence (leading/trailing whitespace on
    the fence line is tolerated). The block ends at the next ``---`` line, and the
    body is everything after it — kept exactly, so a body that itself contains a
    ``---`` line downstream is untouched.
    """
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != _FENCE:
        raise ValidationError("skill must begin with a YAML frontmatter block delimited by '---'")
    for index in range(1, len(lines)):
        if lines[index].strip() == _FENCE:
            frontmatter_text = "".join(lines[1:index])
            body = "".join(lines[index + 1 :])
            return frontmatter_text, body
    raise ValidationError("skill frontmatter block is not closed with a '---' fence")


def _load_frontmatter(text: str) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValidationError(f"skill frontmatter is not valid YAML: {exc}") from exc
    if loaded is None:
        raise ValidationError("skill frontmatter is empty")
    if not isinstance(loaded, dict):
        raise ValidationError("skill frontmatter must be a mapping of fields")
    return loaded


# ---------------------------------------------------------------------------
# Field validation
# ---------------------------------------------------------------------------


def _require_str(data: dict[str, Any], key: str) -> str:
    if key not in data:
        raise ValidationError(f"skill frontmatter is missing required field {key!r}")
    value = data[key]
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"skill frontmatter field {key!r} must be a non-empty string")
    return value.strip()


def _validate_name(name: str) -> None:
    if not NAME_PATTERN.match(name):
        raise ValidationError(
            f"skill name {name!r} must be kebab-case: lowercase letters, digits and "
            "hyphens, not starting with a hyphen"
        )


def _validate_version(version: str) -> None:
    if not VERSION_PATTERN.match(version):
        raise ValidationError(
            f"skill version {version!r} must be semver MAJOR.MINOR.PATCH, e.g. '1.0.0'"
        )


def _normalise_updated_at(value: Any) -> str | None:
    """Validate the optional ``updated_at`` and return it as an ISO 8601 string."""
    if value is None:
        return None
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, str):
        candidate = value.strip()
        probe = candidate[:-1] + "+00:00" if candidate.endswith("Z") else candidate
        try:
            dt.datetime.fromisoformat(probe)
        except ValueError as exc:
            raise ValidationError(
                f"skill frontmatter field 'updated_at' is not a valid ISO 8601 datetime: {value!r}"
            ) from exc
        return candidate
    raise ValidationError("skill frontmatter field 'updated_at' must be an ISO 8601 string")


# ---------------------------------------------------------------------------
# JSONB safety
# ---------------------------------------------------------------------------


def _json_safe(value: Any) -> Any:
    """Recursively coerce *value* into something ``json.dumps`` can encode.

    YAML yields ``datetime``/``date`` for timestamps and can key a mapping on a
    non-string; both are turned into strings so the result survives the ``jsonb``
    column.
    """
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


def _require_json_serialisable(frontmatter: dict[str, Any]) -> None:
    try:
        json.dumps(frontmatter)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "skill frontmatter contains values that cannot be stored as JSON"
        ) from exc

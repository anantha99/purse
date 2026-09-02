"""Alembic migration environment for Purse.

A package rather than a bare directory so the migration scripts are importable
(the tests assert that the hard-coded embedding width in the C1.3 migration
still matches ``purse.db.models.EMBEDDING_DIM``) and so they ship inside the
wheel. Alembic ignores ``__init__.py`` when it looks for revisions.
"""

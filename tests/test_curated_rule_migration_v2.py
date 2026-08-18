from __future__ import annotations

import sqlite3

import pytest

from data.relational import SqliteBackend
from scripts.migrate_curated_rules_v2 import (
    export_reviewed_rules,
    import_classified_rules,
)


class _MemoryBackend:
    def __init__(self):
        self.items: list[tuple[str, dict]] = []

    def add(self, content: str, metadata: dict) -> None:
        self.items.append((content, metadata))


def _legacy_chroma(path):
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE embedding_metadata (id INTEGER, key TEXT, string_value TEXT, int_value INTEGER, float_value REAL, bool_value INTEGER);
        CREATE TABLE embedding_fulltext_search_content (id INTEGER, c0 TEXT);
        """)
    rows = [
        (1, "knowledge_type", "curated_rule"),
        (1, "reviewed_by", "qa"),
        (1, "app_package", "com.example.app"),
        (2, "knowledge_type", "curated_rule"),
        (2, "app_package", "com.example.app"),
        (3, "knowledge_type", "experience"),
        (3, "reviewed_by", "qa"),
    ]
    connection.executemany(
        "INSERT INTO embedding_metadata (id, key, string_value) VALUES (?, ?, ?)", rows
    )
    connection.executemany(
        "INSERT INTO embedding_fulltext_search_content (id, c0) VALUES (?, ?)",
        [(1, "reviewed rule"), (2, "unreviewed rule"), (3, "automatic experience")],
    )
    connection.commit()
    connection.close()


def test_export_only_includes_reviewed_curated_rules(tmp_path):
    source = tmp_path / "chroma.sqlite3"
    _legacy_chroma(source)

    rules = export_reviewed_rules(source)

    assert [rule["source_rule_id"] for rule in rules] == ["1"]
    assert rules[0]["classification"] == ""


def test_import_requires_classification_and_is_idempotent(tmp_path):
    backend = _MemoryBackend()
    database = SqliteBackend(str(tmp_path / "audit.db"))
    rule = {
        "source_rule_id": "11",
        "content": "reviewed constraint",
        "reviewed_by": "qa",
        "app_package": "com.example.app",
        "classification": "constraint",
    }

    assert import_classified_rules([rule], backend, database) == 1
    assert import_classified_rules([rule], backend, database) == 0
    assert len(backend.items) == 1
    assert backend.items[0][1]["knowledge_type"] == "constraint"

    quarantined = {**rule, "source_rule_id": "12", "classification": "quarantine"}
    assert import_classified_rules([quarantined], backend, database) == 0
    assert len(backend.items) == 1

    rule["classification"] = "not-allowed"
    with pytest.raises(ValueError):
        import_classified_rules([{**rule, "source_rule_id": "13"}], backend, database)
    database._conn.close()

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from data.relational import SqliteBackend
from data.vector_store import ChromaBackend, VectorStoreBackend

CLASSIFICATIONS = {"constraint", "negative_knowledge", "semantic_hint"}
QUARANTINE = "quarantine"
CURATED_TYPES = {"curated_rule", "app_precondition", "global_knowledge"}


def _repository_root() -> Path:
    return REPOSITORY_ROOT


def _default_source_db() -> Path:
    return _repository_root() / "storage" / "knowledge" / "chroma.sqlite3"


def _metadata_by_id(
    connection: sqlite3.Connection, embedding_id: int
) -> dict[str, str]:
    rows = connection.execute(
        """
        SELECT key, COALESCE(string_value, int_value, float_value, bool_value, '')
        FROM embedding_metadata
        WHERE id = ?
        """,
        (embedding_id,),
    ).fetchall()
    return {str(key): str(value or "") for key, value in rows}


def export_reviewed_rules(source_db: Path) -> list[dict[str, Any]]:
    """Return only audited manual rules; legacy automatic data is intentionally omitted."""
    connection = sqlite3.connect(source_db)
    try:
        rows = connection.execute("""
            SELECT DISTINCT id
            FROM embedding_metadata
            WHERE key = 'knowledge_type'
            """).fetchall()
        exported: list[dict[str, Any]] = []
        for (embedding_id,) in rows:
            metadata = _metadata_by_id(connection, int(embedding_id))
            if metadata.get("knowledge_type") not in CURATED_TYPES:
                continue
            reviewed_by = metadata.get("reviewed_by", "").strip()
            if not reviewed_by:
                continue
            content_row = connection.execute(
                "SELECT c0 FROM embedding_fulltext_search_content WHERE id = ?",
                (embedding_id,),
            ).fetchone()
            content = str(content_row[0] or "") if content_row else ""
            if not content:
                continue
            exported.append(
                {
                    "source_rule_id": str(embedding_id),
                    "content": content,
                    "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "app_package": metadata.get("app_package", ""),
                    "scope": metadata.get("scope", "app"),
                    "reviewed_by": reviewed_by,
                    "domain": metadata.get("domain", ""),
                    "scenario": metadata.get("scenario", ""),
                    "app_version": metadata.get("app_version", ""),
                    "last_verified_at": metadata.get("last_verified_at", ""),
                    "classification": "",
                }
            )
        return sorted(exported, key=lambda item: int(item["source_rule_id"]))
    finally:
        connection.close()


def load_manifest(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Migration manifest must be a JSON list.")
    return [item for item in payload if isinstance(item, dict)]


def import_classified_rules(
    rules: list[dict[str, Any]],
    backend: VectorStoreBackend,
    audit_db: SqliteBackend,
) -> int:
    """Import human-reviewed, explicitly classified rules and record idempotent audit rows."""
    imported = 0
    for rule in rules:
        classification = str(rule.get("classification", "")).strip()
        reviewed_by = str(rule.get("reviewed_by", "")).strip()
        content = str(rule.get("content", "")).strip()
        source_rule_id = str(rule.get("source_rule_id", "")).strip()
        if not classification:
            continue
        if classification == QUARANTINE:
            continue
        if classification not in CLASSIFICATIONS:
            raise ValueError(
                f"Invalid classification for {source_rule_id}: {classification}"
            )
        if not reviewed_by or not content or not source_rule_id:
            raise ValueError(
                f"Incomplete reviewed rule: {source_rule_id or '<missing id>'}"
            )

        existing = audit_db.select(
            "curated_rule_migration_audit", {"source_rule_id": source_rule_id}, limit=1
        )
        if existing:
            continue

        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        metadata = {
            "app_package": str(rule.get("app_package", "")),
            "knowledge_type": classification,
            "source_knowledge_type": "curated_rule",
            "source_rule_id": source_rule_id,
            "reviewed_by": reviewed_by,
            "scope": str(rule.get("scope", "app")),
            "domain": str(rule.get("domain", "")),
            "scenario": str(rule.get("scenario", "")),
            "app_version": str(rule.get("app_version", "")),
            "last_verified_at": str(rule.get("last_verified_at", "")),
            "migrated_at": datetime.now().isoformat(),
        }
        backend.add(content, metadata)
        audit_db.insert(
            "curated_rule_migration_audit",
            {
                "source_rule_id": source_rule_id,
                "content_hash": content_hash,
                "app_package": metadata["app_package"],
                "reviewed_by": reviewed_by,
                "classification": classification,
                "target_rule_id": content_hash,
                "imported_at": metadata["migrated_at"],
            },
        )
        imported += 1
    return imported


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export audited curated rules for classification, then import only approved entries into RAG v2."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    export_parser = commands.add_parser(
        "export", help="Export reviewed legacy rules to a classification manifest"
    )
    export_parser.add_argument("--source-db", type=Path, default=_default_source_db())
    export_parser.add_argument("--output", type=Path, required=True)
    import_parser = commands.add_parser(
        "import", help="Import classified rules into an empty v2 knowledge directory"
    )
    import_parser.add_argument("--manifest", type=Path, required=True)
    import_parser.add_argument("--target-dir", type=Path, required=True)
    import_parser.add_argument("--audit-db", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "export":
        rules = export_reviewed_rules(args.source_db)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"Exported {len(rules)} reviewed rules to {args.output}")
        print(
            "Classify each entry as constraint, negative_knowledge, semantic_hint, or quarantine before import."
        )
        return 0

    rules = load_manifest(args.manifest)
    unclassified = [
        rule.get("source_rule_id", "")
        for rule in rules
        if not rule.get("classification")
    ]
    if unclassified:
        print(f"ERROR: {len(unclassified)} rules remain unclassified; import refused.")
        return 2
    backend = ChromaBackend(persist_dir=str(args.target_dir))
    audit_db = SqliteBackend(str(args.audit_db))
    try:
        imported = import_classified_rules(rules, backend, audit_db)
    finally:
        audit_db._conn.close()
    print(f"Imported {imported} reviewed rules into {args.target_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

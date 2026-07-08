from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

TYPE_ALIASES = ("curated_rule", "app_precondition", "global_knowledge")
LEGACY_TYPES = ("app_precondition", "global_knowledge")
MIGRATIONS = {
    "联想计算器历史记录": "com.zui.calculator",
    "联想计算器复制粘贴": "com.zui.calculator",
    "ZUI桌面模式判断": "com.zui.launcher",
}


@dataclass
class RuleRecord:
    embedding_id: int
    content: str
    app_package: str
    scope: str
    knowledge_type: str


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _db_path() -> Path:
    return _repo_root() / "storage" / "knowledge" / "chroma.sqlite3"


def _placeholders(n: int) -> str:
    return ",".join("?" for _ in range(n))


def _curated_ids(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute(
        f"""
        SELECT DISTINCT id
        FROM embedding_metadata
        WHERE key='knowledge_type' AND string_value IN ({_placeholders(len(TYPE_ALIASES))})
        ORDER BY id
        """,
        TYPE_ALIASES,
    ).fetchall()
    return [int(r[0]) for r in rows]


def _load_curated_records(conn: sqlite3.Connection) -> list[RuleRecord]:
    ids = _curated_ids(conn)
    if not ids:
        return []
    ph = _placeholders(len(ids))
    meta_rows = conn.execute(
        f"""
        SELECT id, key, string_value
        FROM embedding_metadata
        WHERE id IN ({ph}) AND key IN ('app_package', 'scope', 'knowledge_type')
        """,
        ids,
    ).fetchall()
    content_rows = conn.execute(
        f"""
        SELECT id, c0
        FROM embedding_fulltext_search_content
        WHERE id IN ({ph})
        """,
        ids,
    ).fetchall()

    meta_map: dict[int, dict[str, str]] = {eid: {} for eid in ids}
    for row in meta_rows:
        meta_map[int(row[0])][str(row[1])] = str(row[2] or "")
    content_map = {int(row[0]): str(row[1] or "") for row in content_rows}

    records: list[RuleRecord] = []
    for eid in ids:
        meta = meta_map.get(eid, {})
        records.append(
            RuleRecord(
                embedding_id=eid,
                content=content_map.get(eid, ""),
                app_package=meta.get("app_package", ""),
                scope=meta.get("scope", ""),
                knowledge_type=meta.get("knowledge_type", ""),
            )
        )
    return records


def _stats(records: list[RuleRecord]) -> tuple[Counter[str], Counter[str]]:
    by_pkg = Counter(r.app_package for r in records)
    by_scope = Counter(r.scope for r in records)
    return by_pkg, by_scope


def _upsert_metadata_key(
    conn: sqlite3.Connection, embedding_id: int, key: str, value: str
) -> None:
    cur = conn.execute(
        """
        UPDATE embedding_metadata
        SET string_value=?, int_value=NULL, float_value=NULL, bool_value=NULL
        WHERE id=? AND key=?
        """,
        (value, embedding_id, key),
    )
    if cur.rowcount == 0:
        conn.execute(
            """
            INSERT INTO embedding_metadata (id, key, string_value, int_value, float_value, bool_value)
            VALUES (?, ?, ?, NULL, NULL, NULL)
            """,
            (embedding_id, key, value),
        )


def _match_target_pkg(content: str) -> str:
    text = (content or "").strip()
    for prefix, pkg in MIGRATIONS.items():
        if text.startswith(prefix):
            return pkg
    return ""


def _backup_db(db_file: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = db_file.with_name(f"{db_file.name}.bak_{ts}")
    shutil.copy2(db_file, backup)
    return backup


def _print_stats(title: str, records: list[RuleRecord]) -> None:
    by_pkg, by_scope = _stats(records)
    print(f"\n{title}")
    print(f"- total curated rules: {len(records)}")
    print("- by app_package:")
    for pkg, cnt in sorted(by_pkg.items(), key=lambda x: (x[0], x[1])):
        print(f"  {repr(pkg)}: {cnt}")
    print("- by scope:")
    for scope, cnt in sorted(by_scope.items(), key=lambda x: (x[0], x[1])):
        print(f"  {repr(scope)}: {cnt}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate misclassified global curated rules into app namespaces."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Preview only")
    group.add_argument("--apply", action="store_true", help="Apply migration")
    parser.add_argument(
        "--db",
        default=str(_db_path()),
        help="Path to chroma sqlite database",
    )
    args = parser.parse_args()

    db_file = Path(args.db).resolve()
    if not db_file.exists():
        print(f"ERROR: DB not found: {db_file}")
        return 2

    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    try:
        before = _load_curated_records(conn)
        _print_stats("Before", before)

        global_candidates = [
            r
            for r in before
            if r.app_package == "" and r.knowledge_type in TYPE_ALIASES
        ]
        planned_moves: list[tuple[RuleRecord, str]] = []
        for r in global_candidates:
            target = _match_target_pkg(r.content)
            if target:
                planned_moves.append((r, target))

        print("\nPlanned moves:")
        if not planned_moves:
            print("- none")
        for r, target in planned_moves:
            snippet = r.content[:80].replace("\n", " ")
            print(
                f"- id={r.embedding_id} pkg='' -> {target} scope={r.scope!r} content={snippet!r}"
            )

        remaining_universal_ids = [
            r.embedding_id
            for r in global_candidates
            if r.embedding_id not in {m[0].embedding_id for m in planned_moves}
        ]
        print(f"\nRemaining universal-candidate count: {len(remaining_universal_ids)}")

        legacy_type_records = [r for r in before if r.knowledge_type in LEGACY_TYPES]
        print(f"Legacy type rows to normalize: {len(legacy_type_records)}")

        if args.dry_run:
            return 0

        backup = _backup_db(db_file)
        print(f"\nBackup created: {backup}")

        try:
            conn.execute("BEGIN")
            for r, target in planned_moves:
                _upsert_metadata_key(conn, r.embedding_id, "app_package", target)
                _upsert_metadata_key(conn, r.embedding_id, "scope", "app")
                _upsert_metadata_key(
                    conn, r.embedding_id, "knowledge_type", "curated_rule"
                )

            for eid in remaining_universal_ids:
                _upsert_metadata_key(conn, eid, "scope", "universal")
                _upsert_metadata_key(conn, eid, "knowledge_type", "curated_rule")

            # 全量归一化：遗留类型一律收敛到 curated_rule；并补齐缺失 scope
            for r in legacy_type_records:
                _upsert_metadata_key(
                    conn, r.embedding_id, "knowledge_type", "curated_rule"
                )
                if r.app_package:
                    if not str(r.scope or "").strip():
                        _upsert_metadata_key(conn, r.embedding_id, "scope", "app")
                else:
                    if not str(r.scope or "").strip():
                        _upsert_metadata_key(conn, r.embedding_id, "scope", "universal")

            conn.commit()
        except Exception:
            conn.rollback()
            raise

        after = _load_curated_records(conn)
        _print_stats("After", after)

        changed_ids = {
            *[r.embedding_id for r, _ in planned_moves],
            *remaining_universal_ids,
            *[r.embedding_id for r in legacy_type_records],
        }
        changed = len(changed_ids)
        print(f"\nChanged rule count: {changed}")
        if changed == 0:
            print("No changes applied. Exiting with code 1.")
            return 1
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())

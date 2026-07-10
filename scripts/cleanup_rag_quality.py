"""RAG 知识库质量清理：合并重复规则 + 删除低质经验 + 清除 auto 规则。"""
import sys
import re
import hashlib

sys.path.insert(0, ".")
from data import create_vector_store
from data.knowledge import KnowledgeBase
from config import TestConfig


def _sig_auto_curated(meta: dict, content: str) -> str:
    """auto_exact_click 规则的聚合签名：app|class|path。"""
    app = meta.get("app_package", "")
    cls = ""
    m = re.search(r"class=(\S+)", content)
    if m:
        cls = m.group(1)
    path = ""
    m = re.search(r"path=(\S+)", content)
    if m:
        path = m.group(1)
    return f"{app}|{cls}|{path}"


def main(dry_run: bool = True, purge_auto: bool = False):
    store = create_vector_store(TestConfig())
    kb = KnowledgeBase(store)
    print(f"Total before: {kb.count}")

    deleted_curated = 0
    deleted_exp = 0
    deleted_purge = 0

    # ── 模式 1: purge-auto — 全量删除 auto_exact_click 人工知识 ──
    if purge_auto:
        curated = kb.list_entries(knowledge_type="curated_rule", top_k=500)
        for r in curated:
            meta = r.get("metadata", {}) or {}
            scenario = str(meta.get("scenario", ""))
            if scenario.startswith("auto_"):
                rid_val = r.get("id", "")
                content = str(r.get("content", ""))[:80]
                if dry_run:
                    print(f"[DRY-RUN] Would purge auto curated: [{scenario}] {content}")
                else:
                    store.delete_by_ids([rid_val])
                deleted_purge += 1
        print(f"Auto curated purged: {deleted_purge}")

    # ── 模式 2: dedup — 合并 auto 规则内重复（同 app+class+path 只留一条） ──
    if not purge_auto:
        curated = kb.list_entries(knowledge_type="curated_rule", top_k=500)
        sigs = {}
        for r in curated:
            meta = r.get("metadata", {}) or {}
            if meta.get("scenario") != "auto_exact_click":
                continue
            content = str(r.get("content", ""))
            sig = _sig_auto_curated(meta, content)
            rid_val = r.get("id", "")
            if sig in sigs:
                if dry_run:
                    print(f"[DRY-RUN] Would delete dup curated: {content[:80]}")
                else:
                    store.delete_by_ids([rid_val])
                deleted_curated += 1
            else:
                sigs[sig] = rid_val
        print(f"Dup curated deleted: {deleted_curated}")

    # ── 3. 清理 experience：旧格式（page 含 hash）+ 重复 ──
    exps = kb.list_entries(knowledge_type="experience", top_k=500)
    exp_sigs = {}
    for r in exps:
        meta = r.get("metadata", {}) or {}
        content = str(r.get("content", ""))
        pn = meta.get("page_norm", "") or kb._normalize_page_id(content.split(" → ")[0])
        an = meta.get("action_label_norm", "") or ""
        rt = meta.get("rid_tail", "") or ""
        sig = f"{pn}|{an}|{rt}"
        # 含 hash 的旧格式
        if "#" in content.split(" → ")[0]:
            if dry_run:
                print(f"[DRY-RUN] Would delete old-format exp: {content[:80]}")
            else:
                store.delete_by_ids([r.get("id", "")])
            deleted_exp += 1
            continue
        # 重复
        rid_val = r.get("id", "")
        if sig in exp_sigs:
            if dry_run:
                print(f"[DRY-RUN] Would delete dup exp: {content[:60]}")
            else:
                store.delete_by_ids([rid_val])
            deleted_exp += 1
        else:
            exp_sigs[sig] = rid_val
    print(f"Experience deleted: {deleted_exp}")

    total_deleted = deleted_curated + deleted_exp + deleted_purge
    print(f"Total after: {kb.count - total_deleted}")
    if dry_run:
        print("DRY-RUN mode. Add --apply to execute.")
    else:
        print("Cleanup completed.")


if __name__ == "__main__":
    dry = "--apply" not in sys.argv
    purge = "--purge-auto" in sys.argv
    print(f"Mode: {'PURGE-AUTO' if purge else 'DEDUP'} | {'DRY-RUN' if dry else 'APPLY'}")
    main(dry_run=dry, purge_auto=purge)

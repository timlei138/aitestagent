"""P2 存量清理：合并重复规则 + 删除低质经验。dry-run 模式先预览。"""
import sys, hashlib

sys.path.insert(0, ".")
from data import create_vector_store
from data.knowledge import KnowledgeBase
from config import TestConfig


def main(dry_run: bool = True):
    store = create_vector_store(TestConfig())
    kb = KnowledgeBase(store)
    print(f"Total before: {kb.count}")
    deleted_curated = 0
    deleted_exp = 0

    # 1. 合并 curated 重复（同 app + 同 class + 同 path 的 auto 规则只保留一条）
    curated = kb.list_entries(knowledge_type="curated_rule", top_k=100)
    sigs = {}
    for r in curated:
        meta = r.get("metadata", {}) or {}
        if meta.get("scenario") != "auto_exact_click":
            continue
        content = str(r.get("content", ""))
        app = meta.get("app_package", "")
        cls = ""
        path = ""
        import re
        m = re.search(r"class=(\S+)", content)
        if m:
            cls = m.group(1)
        m = re.search(r"path=(\S+)", content)
        if m:
            path = m.group(1)
        sig = f"{app}|{cls}|{path}"
        rid_val = r.get("id", "")
        if sig in sigs:
            if dry_run:
                print(f"[DRY-RUN] Would delete dup curated: {content[:80]}")
            else:
                store.delete_by_ids([rid_val])
            deleted_curated += 1
        else:
            sigs[sig] = rid_val

    # 2. 删除 experience 旧格式（page 含 hash）或重复
    exps = kb.list_entries(knowledge_type="experience", top_k=200)
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

    print(f"Curated deleted: {deleted_curated}")
    print(f"Experience deleted: {deleted_exp}")
    print(f"Total after: {kb.count - deleted_curated - deleted_exp}")
    if dry_run:
        print("DRY-RUN mode. Run with: python scripts/cleanup_rag_quality.py --apply")


if __name__ == "__main__":
    dry = "--apply" not in sys.argv
    main(dry_run=dry)

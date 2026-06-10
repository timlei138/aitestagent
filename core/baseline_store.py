from __future__ import annotations

import hashlib
import json
import os
from glob import glob
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4

import numpy as np


@dataclass
class PageBaseline:
    app_package: str
    page_key: str
    page_name: str
    screenshot_path: str
    screenshot_hash: str
    screenshot_phash: str
    ui_tree_hash: str
    element_count: int
    clickable_count: int
    elements: list[dict[str, Any]] = field(default_factory=list)
    activity_name: str = ""
    parent_page: str = ""
    action_to_reach: str = ""
    traversal_depth: int = 0
    white_pixel_ratio: float = 0.0
    black_pixel_ratio: float = 0.0
    timestamp: str = ""
    run_id: str = ""


class BaselineStore:
    def __init__(self, storage_dir: str = "storage/baselines"):
        self.storage_dir = storage_dir
        os.makedirs(storage_dir, exist_ok=True)
        self._active_runs: dict[str, str] = {}

    def save_page(
        self,
        app_package: str,
        page_key: str,
        page_name: str,
        screenshot,
        ui_tree_xml: str,
        elements: list[dict[str, Any]],
        activity_name: str = "",
        parent_page: str = "",
        action_to_reach: str = "",
        traversal_depth: int = 0,
        run_id: str = "",
    ) -> PageBaseline:
        from imagehash import phash

        app_dir = self._app_dir(app_package, run_id=run_id, create=True)
        resolved_run_id = self._run_id_for_dir(app_package, app_dir)
        safe_key = self._safe(page_key)
        screenshot_path = os.path.join(app_dir, "pages", f"{safe_key}.png")
        screenshot.save(screenshot_path)

        with open(screenshot_path, "rb") as f:
            screenshot_hash = hashlib.md5(f.read()).hexdigest()
        ui_hash = hashlib.md5(ui_tree_xml.encode("utf-8")).hexdigest()
        phash_value = str(phash(screenshot))
        arr = np.array(screenshot)
        white_ratio = float(np.mean(np.all(arr > 240, axis=2)))
        black_ratio = float(np.mean(np.all(arr < 15, axis=2)))

        baseline = PageBaseline(
            app_package=app_package,
            page_key=page_key,
            page_name=page_name,
            screenshot_path=screenshot_path,
            screenshot_hash=screenshot_hash,
            screenshot_phash=phash_value,
            ui_tree_hash=ui_hash,
            element_count=len(elements),
            clickable_count=sum(1 for e in elements if e.get("clickable")),
            elements=elements,
            activity_name=activity_name,
            parent_page=parent_page,
            action_to_reach=action_to_reach,
            traversal_depth=traversal_depth,
            white_pixel_ratio=white_ratio,
            black_pixel_ratio=black_ratio,
            timestamp=datetime.now().isoformat(),
            run_id=resolved_run_id,
        )

        with open(
            os.path.join(app_dir, "pages", f"{safe_key}.json"), "w", encoding="utf-8"
        ) as f:
            json.dump(asdict(baseline), f, ensure_ascii=False, indent=2)
        self._update_manifest(app_package, page_key, run_id=resolved_run_id)
        self._update_phash_index(
            app_package, page_key, phash_value, run_id=resolved_run_id
        )
        return baseline

    def load_page(
        self, app_package: str, page_key: str, run_id: str = ""
    ) -> PageBaseline | None:
        path = os.path.join(
            self._app_dir(app_package, run_id=run_id), "pages", f"{self._safe(page_key)}.json"
        )
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return PageBaseline(**json.load(f))

    def list_pages(self, app_package: str, run_id: str = "") -> list[str]:
        manifest = self.load_manifest(app_package, run_id=run_id)
        if not manifest:
            return []
        keys = manifest.get("page_keys", [])
        return [k for k in keys if self._is_page_key_for_package(k, app_package)]

    def load_manifest(self, app_package: str, run_id: str = "") -> dict[str, Any] | None:
        path = os.path.join(self._app_dir(app_package, run_id=run_id), "manifest.json")
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def find_best_match(
        self, app_package: str, screenshot, threshold: int = 15, run_id: str = ""
    ) -> PageBaseline | None:
        from imagehash import hex_to_hash, phash

        index_path = os.path.join(
            self._app_dir(app_package, run_id=run_id), "phash_index.json"
        )
        if not os.path.exists(index_path):
            return None
        with open(index_path, "r", encoding="utf-8") as f:
            index = json.load(f)
        current = phash(screenshot)
        best_key = ""
        best_distance = 999
        for key, value in index.items():
            distance = current - hex_to_hash(value)
            if distance < best_distance:
                best_key, best_distance = key, distance
        if best_key and best_distance <= threshold:
            return self.load_page(app_package, best_key, run_id=run_id)
        return None

    def set_active_run(self, app_package: str, run_id: str) -> str:
        normalized_pkg = app_package or "unknown"
        normalized_run_id = (run_id or uuid4().hex).strip()
        self._active_runs[normalized_pkg] = normalized_run_id
        self._app_dir(normalized_pkg, run_id=normalized_run_id, create=True)
        return normalized_run_id

    def get_active_run(self, app_package: str) -> str:
        return self._active_runs.get(app_package or "unknown", "")

    def latest_run_id(self, app_package: str) -> str:
        app_root = self._app_root(app_package)
        if not os.path.exists(app_root):
            return ""
        candidates: list[tuple[float, str]] = []
        for name in os.listdir(app_root):
            full = os.path.join(app_root, name)
            if not os.path.isdir(full):
                continue
            manifest = os.path.join(full, "manifest.json")
            if not os.path.exists(manifest):
                continue
            try:
                mtime = os.path.getmtime(manifest)
            except OSError:
                mtime = 0.0
            candidates.append((mtime, name))
        if not candidates:
            return ""
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    def get_run_dir(self, app_package: str, run_id: str = "") -> str:
        return self._app_dir(app_package, run_id=run_id, create=True)

    def _app_root(self, app_package: str) -> str:
        return os.path.join(self.storage_dir, app_package or "unknown")

    def _app_dir(self, app_package: str, run_id: str = "", create: bool = False) -> str:
        app_root = self._app_root(app_package)
        resolved_run = (run_id or "").strip()
        if not resolved_run:
            resolved_run = self.get_active_run(app_package)
        if not resolved_run:
            resolved_run = self.latest_run_id(app_package)
        path = os.path.join(app_root, resolved_run) if resolved_run else app_root
        if create:
            os.makedirs(os.path.join(path, "pages"), exist_ok=True)
        return path

    def _run_id_for_dir(self, app_package: str, app_dir: str) -> str:
        app_root = os.path.abspath(self._app_root(app_package))
        resolved = os.path.abspath(app_dir)
        if resolved == app_root:
            return ""
        rel = os.path.relpath(resolved, app_root)
        if rel.startswith(".."):
            return ""
        return rel.split(os.sep)[0]

    def _safe(self, value: str) -> str:
        return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in value)[:180]

    def _update_manifest(self, app_package: str, page_key: str, run_id: str = "") -> None:
        path = os.path.join(self._app_dir(app_package, run_id=run_id, create=True), "manifest.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {
                "app_package": app_package,
                "page_keys": [],
                "created_at": datetime.now().isoformat(),
            }
        if page_key not in data["page_keys"]:
            data["page_keys"].append(page_key)
        data["page_keys"] = [
            k for k in data["page_keys"] if self._is_page_key_for_package(k, app_package)
        ]
        if run_id:
            data["run_id"] = run_id
        data["total_pages"] = len(data["page_keys"])
        data["updated_at"] = datetime.now().isoformat()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _update_phash_index(
        self, app_package: str, page_key: str, phash_value: str, run_id: str = ""
    ) -> None:
        path = os.path.join(
            self._app_dir(app_package, run_id=run_id, create=True), "phash_index.json"
        )
        data = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        if self._is_page_key_for_package(page_key, app_package):
            data[page_key] = phash_value
        data = {k: v for k, v in data.items() if self._is_page_key_for_package(k, app_package)}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def remove_pages_by_activity_keywords(
        self, app_package: str, keywords: list[str], run_id: str = ""
    ) -> list[str]:
        """按 activity 关键字清理页面基线（如 LeakCanary 启动页）。"""
        removed_keys: list[str] = []
        lowered = [k.lower() for k in (keywords or []) if k]
        if not lowered:
            return removed_keys

        for app_dir in self._iter_target_dirs(app_package, run_id=run_id):
            pages_dir = os.path.join(app_dir, "pages")
            if not os.path.exists(pages_dir):
                continue
            for json_path in glob(os.path.join(pages_dir, "*.json")):
                try:
                    with open(json_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    activity = str(data.get("activity_name", "")).lower()
                    if not any(k in activity for k in lowered):
                        continue
                    page_key = str(data.get("page_key", ""))
                    screenshot_path = data.get("screenshot_path")

                    if os.path.exists(json_path):
                        os.remove(json_path)
                    if screenshot_path and os.path.exists(screenshot_path):
                        os.remove(screenshot_path)
                    if page_key:
                        removed_keys.append(page_key)
                except Exception:
                    continue

        if not removed_keys:
            return removed_keys

        self._cleanup_indexes(app_package, removed_keys, run_id=run_id)

        return removed_keys

    def sanitize_page_elements_by_package(
        self,
        app_package: str,
        ignore_packages: list[str] | None = None,
        run_id: str = "",
    ) -> int:
        """清理页面基线元素，仅保留目标 App 包元素。"""
        ignored = set((ignore_packages or []))
        target = (app_package or "").lower()
        changed = 0
        for app_dir in self._iter_target_dirs(app_package, run_id=run_id):
            pages_dir = os.path.join(app_dir, "pages")
            if not os.path.exists(pages_dir):
                continue
            for json_path in glob(os.path.join(pages_dir, "*.json")):
                try:
                    with open(json_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    elements = data.get("elements", []) or []
                    old_count = len(elements)
                    kept: list[dict[str, Any]] = []
                    for el in elements:
                        pkg = str(el.get("package", "")).lower().strip()
                        if pkg in ignored:
                            continue
                        if pkg and target and not (pkg == target or pkg.startswith(f"{target}.")):
                            continue
                        kept.append(el)

                    if len(kept) == old_count:
                        continue
                    data["elements"] = kept
                    data["element_count"] = len(kept)
                    data["clickable_count"] = sum(1 for e in kept if e.get("clickable"))
                    with open(json_path, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                    changed += 1
                except Exception:
                    continue
        return changed

    def remove_foreign_pages(
        self,
        app_package: str,
        ignore_packages: list[str] | None = None,
        run_id: str = "",
    ) -> int:
        """移除非目标 App 页面基线及索引，避免混入系统/其他应用页面。"""
        removed_keys: list[str] = []
        removed_files = 0
        ignored = set((ignore_packages or []))
        target = (app_package or "").lower().strip()
        for app_dir in self._iter_target_dirs(app_package, run_id=run_id):
            pages_dir = os.path.join(app_dir, "pages")
            if not os.path.exists(pages_dir):
                continue
            for json_path in glob(os.path.join(pages_dir, "*.json")):
                try:
                    with open(json_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    page_key = str(data.get("page_key", ""))
                    pkg = self._extract_package_from_page_key(page_key)
                    if not pkg:
                        pkg = str(data.get("app_package", "")).lower().strip()
                    if pkg in ignored:
                        should_remove = True
                    elif target and pkg and not (pkg == target or pkg.startswith(f"{target}.")):
                        should_remove = True
                    else:
                        should_remove = False
                    if not should_remove:
                        continue
                    screenshot_path = data.get("screenshot_path")
                    if os.path.exists(json_path):
                        os.remove(json_path)
                    if screenshot_path and os.path.exists(screenshot_path):
                        os.remove(screenshot_path)
                    if page_key:
                        removed_keys.append(page_key)
                    removed_files += 1
                except Exception:
                    continue
        self._cleanup_indexes(app_package, removed_keys, run_id=run_id)
        return removed_files

    def _iter_target_dirs(self, app_package: str, run_id: str = "") -> list[str]:
        if run_id:
            return [self._app_dir(app_package, run_id=run_id, create=True)]
        app_root = self._app_root(app_package)
        dirs: list[str] = []
        legacy_pages = os.path.join(app_root, "pages")
        if os.path.isdir(legacy_pages):
            dirs.append(app_root)
        if os.path.exists(app_root):
            for name in os.listdir(app_root):
                full = os.path.join(app_root, name)
                if not os.path.isdir(full):
                    continue
                if os.path.isdir(os.path.join(full, "pages")):
                    dirs.append(full)
        if not dirs:
            dirs.append(self._app_dir(app_package, run_id=run_id, create=True))
        return dirs

    def _cleanup_indexes(self, app_package: str, removed_keys: list[str], run_id: str = "") -> None:
        for app_dir in self._iter_target_dirs(app_package, run_id=run_id):
            manifest_path = os.path.join(app_dir, "manifest.json")
            if os.path.exists(manifest_path):
                try:
                    with open(manifest_path, "r", encoding="utf-8") as f:
                        manifest = json.load(f)
                    page_keys = manifest.get("page_keys", [])
                    manifest["page_keys"] = [
                        k
                        for k in page_keys
                        if k not in removed_keys and self._is_page_key_for_package(k, app_package)
                    ]
                    manifest["total_pages"] = len(manifest["page_keys"])
                    manifest["updated_at"] = datetime.now().isoformat()
                    with open(manifest_path, "w", encoding="utf-8") as f:
                        json.dump(manifest, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass

            phash_path = os.path.join(app_dir, "phash_index.json")
            if os.path.exists(phash_path):
                try:
                    with open(phash_path, "r", encoding="utf-8") as f:
                        phash_index = json.load(f)
                    for key in removed_keys:
                        phash_index.pop(key, None)
                    phash_index = {
                        k: v
                        for k, v in phash_index.items()
                        if self._is_page_key_for_package(k, app_package)
                    }
                    with open(phash_path, "w", encoding="utf-8") as f:
                        json.dump(phash_index, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass

    def _extract_package_from_page_key(self, page_key: str) -> str:
        if "|" in page_key:
            return page_key.split("|", 1)[0].strip().lower()
        return ""

    def _is_page_key_for_package(self, page_key: str, app_package: str) -> bool:
        pkg = self._extract_package_from_page_key(page_key)
        target = (app_package or "").lower().strip()
        if not target:
            return True
        if not pkg:
            return False
        return pkg == target or pkg.startswith(f"{target}.")

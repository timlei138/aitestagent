# 全局知识架构重构计划（2026-07-03 v2）

> v2 变更：合入 Review 反馈，补全写入管控、双路查询、迁移审计、污染哨兵、自动化单测。

## 1. 问题与动机

### 1.1 当前问题
全局知识过滤存在系统性泄漏：与当前 App 无关的规则（如"联想计算器历史记录"、"ZUI桌面模式判断"）被注入到图库任务的 Planner/Agent prompt 中。

实测数据：
- 5 条全局规则中 3 条泄漏（60%），目标 < 20%
- MIN_SCORE 从 2 提到 3 后过滤了 2 条，但剩余规则通过泛化词（"选择"、"系统"）凑分通过
- 评分函数 `_global_rule_relevance` 本质是词袋匹配，无法区分"图库的选择"和"计算器的选择"

### 1.2 根因
不是过滤逻辑不够好，而是**不该存在的数据被存成了全局知识**：
- "联想计算器历史记录..." → 应为 `com.zui.calculator` 的 App 规则
- "ZUI桌面模式判断..." → 应为 `com.zui.launcher` 的 App 规则
- "联想计算器复制粘贴..." → 应为 `com.zui.calculator` 的 App 规则

### 1.3 设计原则
> **不要过滤危险输入，要从入口和结构上让泄漏不可能发生。**

## 2. 目标架构

```
重构前（泄漏）：
┌─────────────────────────────┐
│  Global Rules (空包名, 5条)  │ ← 查询时全部拉出，靠评分过滤
│  ├─ 计算器长按公式...         │   ↑ 评分器误判 → 泄漏
│  ├─ ZUI桌面模式...            │   ↑ 评分器误判 → 泄漏
│  ├─ 计算器复制粘贴...         │   ↑ 评分器误判 → 泄漏
│  └─ (2条已被MIN_SCORE拦截)    │
└─────────────────────────────┘

重构后（无泄漏可能）：
┌─────────────────────────────┐
│  Universal Rules (空包名)    │ ← 只有真正对所有App通用的规则
│  └─ (当前: 空)               │   查询时直接全量注入，无需过滤
└─────────────────────────────┘
┌─────────────────────────────┐
│  App Rules                  │ ← 按 app_package 精确匹配
│  ├─ com.zui.gallery (3条)   │
│  ├─ com.zui.calculator (3条)│ ← 从全局迁移过来
│  ├─ com.zui.launcher (1条)  │ ← 从全局迁移过来
│  └─ com.android.settings (2条)│
└─────────────────────────────┘
```

查询逻辑从"拉全部 → 评分 → 过滤"变为"取 universal + 当前 App → 直接注入"。

## 3. 具体修改点

### 3.1 数据迁移脚本（新增，一次性，可审计）

新建 `scripts/migrate_global_rules.py`：

```python
"""将错误归类的全局规则迁移到正确的 App namespace。

用法:
  python scripts/migrate_global_rules.py --dry-run   # 预览变更
  python scripts/migrate_global_rules.py --apply     # 执行变更
"""
import argparse
import sqlite3
import shutil
from datetime import datetime

MIGRATIONS = {
    "联想计算器历史记录": "com.zui.calculator",
    "联想计算器复制粘贴": "com.zui.calculator",
    "ZUI桌面模式判断":    "com.zui.launcher",
}
```

执行逻辑：
1. 匹配条件必须包含：`knowledge_type IN ('curated_rule', 'app_precondition', 'global_knowledge')` + `app_package=''`（可再加 `scope='global'`）
2. 按内容前缀在 `embedding_fulltext_search_content` 中定位 embedding ID
3. 在 `embedding_metadata` 中更新 `app_package` 从空 → 目标包名
4. `scope` 做 upsert：已有则 UPDATE，缺失则 INSERT 新行
   - 保留为全局的规则：`scope` 从旧值（`"global"` 或空）统一改为 `"universal"`
   - 旧 `scope="global"` 不再被查询端识别，必须全部迁移为 `"universal"`
5. 输出变更前后统计：规则总数、各 pkg 分布、每条变更明细（id、old_pkg、new_pkg、content 前缀）
6. `--dry-run` 只输出统计不执行，`--apply` 执行并备份 SQLite 文件
7. **事务要求**：`--apply` 必须单事务执行，失败全回滚；变更条数=0 时输出提示并以 exit code 1 退出（避免误认为成功）

幂等设计：已迁移的规则 `app_package` 不为空，不会被重复匹配。

### 3.2 `data/knowledge.py` — `query_curated_rules` 改为双路精确查询

**修改位置**：L228-286

**当前代码**（~60行）：
```python
def query_curated_rules(self, app_package, user_request="", top_k=5):
    all_results = self.query("", knowledge_type="curated_rule", top_k=top_k * 2)
    # ... 全量拉取 + Python 侧分组 + 评分过滤 ...
```

**重构后代码**（~35行，双路精确拉取 + 旧类型兼容）：
```python
def query_curated_rules(self, app_package: str, user_request: str = "", top_k: int = 5) -> str:
    """查询人工知识：双路精确 metadata 查询，无评分过滤。"""
    # 旧类型兼容：查询时走 _TYPE_ALIASES 展开为 $or
    # curated_rule / app_precondition / global_knowledge 均可命中
    type_filter = {"$or": [{"knowledge_type": t} for t in self._TYPE_ALIASES.get("curated_rule", ["curated_rule"])]}

    # 路1：当前 App 专属规则（空包名时不走此路，避免与 universal 重复）
    app_lines = []
    if app_package:
        app_rules = self.backend.get_by_metadata(
            {**type_filter, "app_package": app_package},
            limit=top_k,
        )
        app_lines = [f"- {r['content']}" for r in app_rules]

    # 路2：Universal 规则（app_package="" 且 scope="universal"）
    universal_rules = self.backend.get_by_metadata(
        {**type_filter, "app_package": "", "scope": "universal"},
        limit=top_k,
    )
    universal_lines = [f"- {r['content']}" for r in universal_rules]

    parts = []
    if universal_lines:
        parts.append("### 通用知识\n" + "\n".join(universal_lines))
    if app_lines:
        parts.append("### App 操作前提\n" + "\n".join(app_lines))
    return "\n\n".join(parts)
```

优势：
- 两次精确 metadata 查询，不受 top_k 截断影响
- 不需要 Python 侧分组/过滤/评分
- universal 规则必须有 `scope="universal"` 标记才会被查到
- `app_package=""` 时跳过路1，避免与 universal 重复
- 旧类型（`app_precondition`/`global_knowledge`）通过 `$or` 兼容查询


### 3.3 `data/knowledge.py` — 删除不再需要的辅助函数

以下 4 个函数全部删除（~55 行代码）：

| 函数 | 行号 | 说明 |
|------|------|------|
| `_app_tokens()` | L327-331 | 包名分词，仅供评分使用 |
| `_global_rule_relevance()` | L333-356 | 全局规则评分，根因所在 |
| `_request_tokens()` | L358-368 | 请求分词，仅供评分使用 |
| `_infer_app_domains()` | L370-381 | 域推断，仅供 domain 匹配使用 |

同时删除类常量：
- `_GLOBAL_RULE_MIN_SCORE = 3`（L36）

### 3.4 `data/knowledge.py` — `save_curated_rule` 写入管控

**修改位置**：L190-226

默认禁止 `app_package=""` 写入。仅当显式声明 `scope="universal"` 时允许：

```python
def save_curated_rule(
    self, app_package: str, content: str, *,
    scope: str = "app",          # 新增："app" | "universal"
    reviewed_by: str = "",       # 新增：审核人标记
    ...
) -> None:
    if not app_package:
        if scope != "universal":
            raise ValueError(
                f"Global rule requires scope='universal'. Got scope='{scope}'. "
                f"Content: {content[:60]}"
            )
        if not reviewed_by:
            raise ValueError(
                f"Universal rule requires reviewed_by (audit trail). "
                f"Content: {content[:60]}"
            )
        logger.info(
            "Saving universal rule (reviewed_by=%s): %s",
            reviewed_by, content[:60],
        )
    # metadata 中写入 scope
    metadata = {
        "scope": scope,   # "app" 或 "universal"
        ...
    }
```

效果：
- 误写 `app_package=""` 且不带 `scope="universal"` → 直接 `ValueError`
- 写入 universal 规则必须显式声明 `scope="universal"` + `reviewed_by` 非空（审计闭环）
- 与 §3.2 双路查询配合：只有 `scope="universal"` 的规则才会被查到

### 3.5 `agents/graph.py` — `_rag_ctx` 无需修改

**位置**：L575-589

```python
def _rag_ctx(kb, app_package, user_request=""):
    rules = kb.query_curated_rules(app_package, user_request=user_request)
    # ... 不变 ...
```

`_rag_ctx` 只调用 `query_curated_rules`，不感知内部过滤逻辑。接口不变，此处无需改动。

### 3.6 `tools/__init__.py` — `query_app_knowledge` 无需修改

**位置**：L700-721

```python
rule_text = ctx.knowledge_base.query_curated_rules(package, top_k=3)
```

同上，只调用接口，无需修改。

### 3.7 `agents/graph.py` — `page_sig_once` 可选优化

**位置**：L260

```python
page_sig_once = _build_page_signature(_ctx)
```

当前 `_build_page_signature` 仍调用 `perceive()` 生成 MD5 hash，仅供精确重复断路器使用。NO_PROGRESS 断路器已不依赖它。可考虑后续移除，但本轮不做。

## 4. 代码改动量估算

| 文件 | 操作 | 行数变化 |
|------|------|--------|
| `data/knowledge.py` | 重写 `query_curated_rules` 为双路查询 | -35行 / +20行 |
| `data/knowledge.py` | 删除 4 个辅助函数 + 1 常量 | -55行 |
| `data/knowledge.py` | `save_curated_rule` 写入管控 | +10行 |
| `scripts/migrate_global_rules.py` | 新增迁移脚本（dry-run/apply/审计） | +60行 |
| `tests/test_knowledge_no_leak.py` | 新增单测（3 用例 + 哨兵） | +50行 |
| **合计** | | **~-0行（简化+新增平衡）** |

## 5. 数据迁移明细

### 5.1 需迁移的规则

| 规则内容（前缀） | 当前 pkg | 目标 pkg | 原因 |
|----------------|----------|---------|------|
| "联想计算器历史记录..." | (空) | com.zui.calculator | 计算器专属操作 |
| "联想计算器复制粘贴..." | (空) | com.zui.calculator | 计算器专属操作 |
| "ZUI桌面模式判断..." | (空) | com.zui.launcher | 桌面/Launcher 专属 |

### 5.2 需处理的低分规则

日志显示 2 条 score=2 被 drop 的全局规则。迁移后可选：
- **保留为全局**：如果确认是通用知识
- **迁移到对应 App**：如果是特定 App 的
- **删除**：如果已无价值

具体需查看规则内容后决定（迁移脚本中打印全部 5 条全局规则内容供人工审核）。

### 5.3 迁移脚本执行

```bash
# Step 1: 预览变更（不修改数据）
python scripts/migrate_global_rules.py --dry-run

# Step 2: 确认后执行变更
python scripts/migrate_global_rules.py --apply
```

幂等设计：已迁移的规则 `app_package` 不为空，不会被重复匹配。

## 6. 验收标准

### 6.1 自动化单测（新增 `tests/test_knowledge_no_leak.py`）

至少 3 个用例 + 1 个污染哨兵：

```python
class TestKnowledgeNoLeak:
    def test_gallery_only_returns_universal_and_gallery(self):
        """gallery 请求不应出现 calculator/launcher 规则"""
        rules = kb.query_curated_rules("com.zui.gallery")
        assert "calculator" not in rules.lower()
        assert "launcher" not in rules.lower()
        assert "桌面模式" not in rules

    def test_calculator_only_returns_universal_and_calculator(self):
        """calculator 请求不应出现 gallery 规则"""
        rules = kb.query_curated_rules("com.zui.calculator")
        assert "图库" not in rules

    def test_no_universal_does_not_break_app_rules(self):
        """无 universal 规则时，app 规则仍正常返回"""
        rules = kb.query_curated_rules("com.zui.gallery")
        assert "App 操作前提" in rules  # gallery 专属规则仍在

    def test_save_global_without_universal_scope_raises(self):
        """写入管控：空包名 + 非 universal scope 应抛异常"""
        with pytest.raises(ValueError):
            kb.save_curated_rule("", "some rule", scope="app")
```

### 6.2 污染哨兵（集成到 CI / nightly）

每次跑测试时自动检查：
```python
def test_contamination_sentinel():
    """随机抽查多个 App 请求，检查返回规则中是否出现其他 App 的专属内容"""
    APP_KEYWORDS = {
        "com.zui.gallery": ["计算器", "桌面模式", "浏览器"],
        "com.zui.calculator": ["图库", "相机"],
    }
    for pkg, forbidden in APP_KEYWORDS.items():
        rules = kb.query_curated_rules(pkg)
        for kw in forbidden:
            assert kw not in rules, f"CONTAMINATION: {pkg} rules contain '{kw}'"
```

### 6.3 功能验证
- [ ] 图库任务：全局知识泄漏率 = 0%（无 calculator/launcher 规则出现）
- [ ] 计算器任务：能看到迁移后的 calculator 规则（验证迁移正确性）
- [ ] Universal 规则（如果有）：任何 App 任务都能看到

### 6.4 回归验证
- [ ] `budget_violation_count=0` 保持
- [ ] NO_PROGRESS 断路器仍正常触发
- [ ] Planner/Agent prompt 格式无变化（仅全局知识段内容变化）

### 6.5 日志验证
- [ ] 无 `rule_score_detail` / `rule_drop_reason` 日志（评分逻辑已删）
- [ ] `save_curated_rule` 写入 universal 规则时有 `reviewed_by` 日志

## 7. 回滚方案

- 代码：`git revert`（改动集中在 knowledge.py，无跨文件耦合）
- 数据：迁移脚本执行前备份 `storage/knowledge/chroma.sqlite3`，回滚时还原

## 8. 后续扩展（不在本轮范围）

1. **增加图库经验**（方案 X）：丰富 `ensure_gallery_seed_knowledge` 的操作经验和规则
2. **Universal 规则审核**：梳理哪些知识真正值得作为全局规则（如权限弹窗处理、Toast 等待）
3. **知识库管理 UI**：在前端 KnowledgePanel 中增加规则迁移/归类功能
4. **namespace_type 数据模型增强**（中期）：给规则增加 `namespace_type: universal|app` 和 `namespace_value`（包名或空），替代当前隐式的 `app_package=""` 语义，使迁移/审计/统计更清晰

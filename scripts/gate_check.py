"""L5 指标门禁：读历史运行指标跑阈值检查，为 legacy 点击代码下线(L2)提供数据依据。

聚合最近 N 次运行的点击质量 / LLM 400 率 / RAG 空命中率 / 完成率，对照下线门槛
输出 PASS/FAIL。只读 test_history.db，不修改任何数据，不联网。

用法:
  python scripts/gate_check.py                  # 最近 20 条
  python scripts/gate_check.py --limit 50
  python scripts/gate_check.py --app com.zui.calculator
  python scripts/gate_check.py --json           # 机器可读输出

退出码：0 = 全部达标(PASS)，1 = 有未达标(FAIL) 或无数据。
"""

import argparse
import json
import sys

sys.path.insert(0, ".")
from data.relational import SqliteBackend  # noqa: E402

# ── 下线门槛（可按需调整；依据 migration §6 的精确模式/兜底率要求）──
THRESHOLDS = {
    "exact_click_rate_min": 0.80,  # 精确定位占比 >=
    "fuzzy_click_rate_max": 0.15,  # 兜底(fallback)点击占比 <=
    "ambiguous_rate_max": 0.10,  # 歧义占比 <=
    "rag_empty_hit_rate_max": 0.40,  # RAG 空命中率 <=
    "completed_rate_min": 0.80,  # 执行完成(completed)率 >=
}


def aggregate(runs: list[dict]) -> dict | None:
    n = len(runs)
    if n == 0:
        return None
    tot_click = sum(int(r.get("click_count", 0) or 0) for r in runs)
    tot_exact = sum(int(r.get("exact_click_count", 0) or 0) for r in runs)
    tot_fuzzy = sum(int(r.get("fuzzy_click_count", 0) or 0) for r in runs)
    tot_amb = sum(int(r.get("ambiguous_count", 0) or 0) for r in runs)
    completed = sum(1 for r in runs if r.get("execution_status") == "completed")
    avg_rag_empty = sum(float(r.get("rag_empty_hit_rate", 0) or 0) for r in runs) / n
    return {
        "runs": n,
        "total_clicks": tot_click,
        "exact_click_rate": round(tot_exact / max(tot_click, 1), 4),
        "fuzzy_click_rate": round(tot_fuzzy / max(tot_click, 1), 4),
        "ambiguous_rate": round(tot_amb / max(tot_click, 1), 4),
        "rag_empty_hit_rate": round(avg_rag_empty, 4),
        "completed_rate": round(completed / n, 4),
    }


def evaluate(agg: dict) -> list[tuple]:
    """返回 [(name, value, op, threshold, ok)]。"""
    spec = [
        ("exact_click_rate", ">=", THRESHOLDS["exact_click_rate_min"]),
        ("fuzzy_click_rate", "<=", THRESHOLDS["fuzzy_click_rate_max"]),
        ("ambiguous_rate", "<=", THRESHOLDS["ambiguous_rate_max"]),
        ("rag_empty_hit_rate", "<=", THRESHOLDS["rag_empty_hit_rate_max"]),
        ("completed_rate", ">=", THRESHOLDS["completed_rate_min"]),
    ]
    checks = []
    for name, op, thr in spec:
        val = agg[name]
        ok = val >= thr if op == ">=" else val <= thr
        checks.append((name, val, op, thr, ok))
    return checks


def main() -> int:
    ap = argparse.ArgumentParser(description="L5 指标门禁检查")
    ap.add_argument("--limit", type=int, default=20, help="统计最近 N 次运行")
    ap.add_argument("--app", default="", help="仅统计某 app_package")
    ap.add_argument("--json", action="store_true", help="机器可读 JSON 输出")
    args = ap.parse_args()

    db = SqliteBackend()
    runs = db.list_test_runs(limit=max(args.limit, 1))
    if args.app:
        runs = [r for r in runs if r.get("app_package") == args.app]

    agg = aggregate(runs)
    if not agg:
        msg = "没有可用的运行记录，无法评估门禁。"
        print(json.dumps({"ok": False, "reason": msg}) if args.json else msg)
        return 1

    checks = evaluate(agg)
    all_ok = all(c[4] for c in checks)

    if args.json:
        print(
            json.dumps(
                {
                    "ok": all_ok,
                    "aggregate": agg,
                    "checks": [
                        {"metric": n, "value": v, "op": op, "threshold": t, "pass": ok}
                        for (n, v, op, t, ok) in checks
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if all_ok else 1

    print(f"\nL5 指标门禁 — 最近 {agg['runs']} 次运行"
          + (f"（app={args.app}）" if args.app else "")
          + f"，累计点击 {agg['total_clicks']} 次\n")
    print(f"{'指标':<22}{'实测':>10}  {'门槛':>12}   结果")
    print("-" * 58)
    for name, val, op, thr, ok in checks:
        print(f"{name:<22}{val:>10}  {op}{thr:>10}   {'PASS' if ok else 'FAIL'}")
    print("-" * 58)
    verdict = "PASS ✅ 满足 legacy 下线数据门槛" if all_ok else "FAIL ❌ 尚未达标，暂不建议下线 legacy"
    print(f"\n总判定：{verdict}\n")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())

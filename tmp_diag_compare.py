import json
from pathlib import Path

def load_trace(rid):
    # Find the latest trace file for run id
    files = sorted(Path("logs/runs").glob(f"*_{rid}_trace.json"))
    if not files:
        return None
    with open(files[-1], encoding="utf-8") as f:
        return json.load(f)

def summarize(trace, label):
    steps = trace.get("steps", [])
    print(f"\n=== {label} (run_id={trace.get('run_id')}, steps={len(steps)}) ===")
    total_ms = sum(s.get("duration_ms", 0) or 0 for s in steps)
    print(f"total step duration_ms: {total_ms} (~{total_ms/1000:.1f}s)")
    # tool distribution
    from collections import Counter
    tools = Counter(s.get("tool") for s in steps)
    print("tool dist:", dict(tools))
    # longest steps
    sorted_steps = sorted(steps, key=lambda s: s.get("duration_ms", 0) or 0, reverse=True)[:8]
    print("top slow steps:")
    for s in sorted_steps:
        print(f"  {s.get('tool'):25} {s.get('duration_ms',0):>6}ms  intent={s.get('intent','')[:80]}")
    # replay fields if any
    replay_steps = [s for s in steps if s.get("replay_source")]
    print(f"replay_source steps: {len(replay_steps)}")
    return steps

rerun = load_trace("test-20260805_104421")
orig = load_trace("test-20260805_103301")
if rerun:
    summarize(rerun, "RERUN (after direct switch)")
if orig:
    summarize(orig, "ORIGINAL normal run")

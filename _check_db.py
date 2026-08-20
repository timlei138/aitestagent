import sqlite3, json

c = sqlite3.connect("storage/test_history.db")
c.row_factory = sqlite3.Row
print("--- recent execution_runs ---")
for row in c.execute("SELECT run_id, created_at, lifecycle_state, verdict, terminal_reason, llm_call_count FROM execution_runs ORDER BY rowid DESC LIMIT 6"):
    print(dict(row))
print("\n--- latest run verification results ---")
r = c.execute("SELECT run_id, verification_results_json FROM execution_runs ORDER BY rowid DESC LIMIT 1").fetchone()
if r:
    print("run_id:", r["run_id"])
    try:
        vj = json.loads(r["verification_results_json"])
        for v in vj:
            print(" ", repr(v.get("key")), repr((v.get("statement") or v.get("claim"))), "->", v.get("status"), "| evidence:", v.get("evidence_count"), "unverified:", v.get("unverified"))
    except Exception as e:
        print("parse err", e, str(r["verification_results_json"])[:800])

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main as _m
from config import TestConfig
from data import create_relational_db
from agents.graph import set_relational_db
from agents.orchestrator import TestOrchestrator

config = TestConfig.from_yaml("config.yaml")
_m._init_tool_context(config)
set_relational_db(create_relational_db(config))
orch = TestOrchestrator(config)
req = open("scripts/_tmp_req.txt", encoding="utf-8").read().strip()
pkg, name = _m._quick_resolve_app(req)
print("RESOLVED", pkg, name, flush=True)

res = orch.start(user_request=req, app_package=pkg, app_name=name)
print("START_STATUS", res.get("status"), "TID", res.get("thread_id"), flush=True)

if res.get("status") == "need_human":
    final = orch.resume(res.get("thread_id"), "confirm")
    print("FINAL_STATUS", final.get("execution_status"), final.get("test_verdict"), flush=True)
else:
    final = res

with open("scripts/_tmp_final.json", "w", encoding="utf-8") as f:
    json.dump(final, f, ensure_ascii=False, indent=2, default=str)

for v in final.get("verification_results", []) or []:
    print("VERIF", v.get("key"), v.get("result"), v.get("item"), flush=True)
print("DRIVE_DONE", flush=True)

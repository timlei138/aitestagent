import sqlite3, json

conn = sqlite3.connect('storage/test_history.db')
row = conn.execute('SELECT steps_json FROM test_runs WHERE id=?', ('test-20260805_100009',)).fetchone()
steps = json.loads(row[0] or '[]')
print('total steps:', len(steps))
for i, s in enumerate(steps):
    at = s.get('action_type')
    if at in ('click', 'type_input', 'vision_tap', 'click_and_check', 'launch_app', 'set_permission_intent', 'assert_verification', 'report_done'):
        ti = s.get('tool_input') or {}
        rt = s.get('resolved_target') or {}
        print(f"{i:3d} {at:22s} status={s.get('status_code','?'):11s} in={json.dumps(ti, ensure_ascii=False)[:70]} resolved={json.dumps(rt, ensure_ascii=False)[:60]} before={str(s.get('page_before_activity',''))[-30:]}")

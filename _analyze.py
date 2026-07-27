import json

t = json.load(open('logs/runs/160350_test-20260727_155716_trace.json', 'r', encoding='utf-8'))
r = t['result']
m = t['metrics']
print(f"Status: {r['execution_status']}")
print(f"Verdict: {r['test_verdict']}")
print(f"Duration: {r['duration_seconds']}s")
print(f"LLM calls: {m['llm_call_count']}, Steps: {t['step_count']}")
print(f"Clicks: {m['click_count']}")
print(f"Tokens: in={t['token_usage']['input_tokens']} out={t['token_usage']['output_tokens']}")
print(f"Cache hit: {t['token_usage']['cached_input_tokens']}")
print()
print("Verifications:")
for v in t['verifications']:
    print(f"  [{v['result']:9s}] {v['key']}: {v['item']}")
    if v['detail']:
        print(f"           {v['detail'][:120]}")
print()
print("Last 15 steps:")
for s in t['steps'][-15:]:
    obs = s.get('observation', '')[:80] if s.get('observation') else ''
    intent = s.get('intent', '')[:60] if s.get('intent') else ''
    print(f"  [{s['seq']:2d}] {s['tool']:25s} target={s.get('target','')[:30]:30s} {s.get('status','')[:6]}")
    if intent:
        print(f"       intent: {intent}")
    if obs:
        print(f"       obs: {obs}")

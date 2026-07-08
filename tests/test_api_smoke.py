from fastapi.testclient import TestClient
from pathlib import Path
import json
import uuid

from api.server import app, _get_relational_db
import app_paths

client = TestClient(app)


def test_run_endpoint_returns_result():
    """新的 /api/run 端点：无需设备即可调用（LLM 在后台报错也返回结构化结果）。"""
    response = client.post(
        "/api/run",
        json={"message": "检查 Settings 的 Wi-Fi 开关", "session_id": "pytest-session"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "status" in data
    assert "data" in data


def test_device_snapshot_endpoint_is_resilient():
    response = client.get("/api/device/snapshot")
    data = response.json()
    # 设备在线→200含screen，设备离线→503含detail
    assert response.status_code in (200, 503)
    assert ("screen" in data) or (data.get("status") == "error") or ("detail" in data)


def test_ws_run_flow():
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"type": "run", "message": "检查 Settings 的 Wi-Fi 开关"})
        msg = ws.receive_json()
        # 至少收到 status 或 result 类型的消息
        assert msg.get("type") in {"status", "result", "error"}


def test_reports_list_endpoint():
    response = client.get("/api/reports/list")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert isinstance(data["items"], list)


def test_report_delete_endpoint_cleans_artifacts():
    db = _get_relational_db()
    assert db is not None
    run_id = f"pytest-delete-{uuid.uuid4().hex[:8]}"
    shot_abs = app_paths.SCREENSHOT_DIR / run_id / "1_test.png"
    shot_abs.parent.mkdir(parents=True, exist_ok=True)
    shot_abs.write_bytes(b"fakepng")
    shot_rel = f"storage/screenshots/{run_id}/1_test.png"

    log_abs = app_paths.LOG_RUN_DIR / f"000000_{run_id}_langchain.log"
    log_abs.parent.mkdir(parents=True, exist_ok=True)
    log_abs.write_text("fake log", encoding="utf-8")

    db.record_test_run(
        run_id=run_id,
        user_request="pytest cleanup",
        app_package="com.demo.app",
        app_name="demo",
        status="fail",
        conclusion="ABORT: pytest",
        steps=[
            {
                "index": 1,
                "action_type": "click",
                "status": "fail",
                "screenshot_path": shot_rel,
            }
        ],
        duration_seconds=1.0,
        execution_status="error",
        test_verdict="inconclusive",
        verification_json=json.dumps(
            [{"item": "x", "result": "failed", "screenshot": shot_rel}],
            ensure_ascii=False,
        ),
    )
    db.insert(
        "human_decisions",
        {"run_id": run_id, "step_index": 1, "question": "q", "decision": "d", "created_at": "pytest"},
    )

    response = client.delete(f"/api/reports/{run_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"

    assert not shot_abs.exists()
    assert not log_abs.exists()
    assert db.get_test_run(run_id) is None
    left = db.execute(
        "SELECT COUNT(*) FROM human_decisions WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    assert left == 0

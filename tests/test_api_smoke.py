import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient
import uuid

from api.server import app, _get_relational_db
import app_paths

client = TestClient(app)
client.get("/")


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
        assert msg.get("type") in {"run_started", "status", "result", "error"}


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
    log_abs = app_paths.LOG_RUN_DIR / f"000000_{run_id}_langchain.log"
    log_abs.parent.mkdir(parents=True, exist_ok=True)
    log_abs.write_text("fake log", encoding="utf-8")

    db.record_execution_run(
        run_id=run_id,
        user_request="pytest cleanup",
        app_package="com.demo.app",
        goal={"goal": "pytest cleanup"},
        verification_contract={"status": "approved", "verifications": []},
        verdict="inconclusive",
        terminal_reason="pytest",
    )

    response = client.delete(f"/api/reports/{run_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"

    assert not shot_abs.exists()
    assert not log_abs.exists()
    assert db.get_execution_run(run_id) is None

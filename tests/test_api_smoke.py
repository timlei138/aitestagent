from fastapi.testclient import TestClient
from pathlib import Path

from api.server import app

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

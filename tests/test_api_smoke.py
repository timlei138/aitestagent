from fastapi.testclient import TestClient
from pathlib import Path

from api.server import app

client = TestClient(app)


def test_parse_endpoint_returns_intent():
    response = client.post(
        "/api/parse",
        json={"message": "全路径扫描 Settings", "session_id": "pytest-session"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "pending_confirmation"
    assert isinstance(data["intent"], dict)
    assert "editable_fields" in data


def test_device_snapshot_endpoint_is_resilient():
    response = client.get("/api/device/snapshot")
    assert response.status_code == 200
    data = response.json()
    # 有设备时返回 screen；无设备时返回结构化 error
    assert ("screen" in data) or (data.get("status") == "error")


def test_ws_parse_flow():
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"type": "parse", "message": "全路径扫描 Settings"})
        first = ws.receive_json()
        second = ws.receive_json()
        types = {first.get("type"), second.get("type")}
        assert "status" in types
        assert "intent" in types


def test_case_content_read_write():
    case_path = Path("test_cases") / "pytest_api_case.yaml"
    case_path.parent.mkdir(parents=True, exist_ok=True)
    case_path.write_text("name: old\nsteps: []\n", encoding="utf-8")
    try:
        read_resp = client.get("/api/cases/content", params={"case_file": str(case_path)})
        assert read_resp.status_code == 200
        read_data = read_resp.json()
        assert read_data["status"] == "success"
        assert "name: old" in read_data["content"]

        save_resp = client.post(
            "/api/cases/content",
            json={"case_file": str(case_path), "content": "name: new\nsteps: []\n"},
        )
        assert save_resp.status_code == 200
        save_data = save_resp.json()
        assert save_data["status"] == "success"
        assert "name: new" in case_path.read_text(encoding="utf-8")
    finally:
        if case_path.exists():
            case_path.unlink()


def test_reports_list_endpoint():
    response = client.get("/api/reports/list")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert isinstance(data["items"], list)

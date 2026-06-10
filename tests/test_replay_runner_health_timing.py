from __future__ import annotations

from core.replay_runner import ReplayRunner


class _Device:
    def __init__(self):
        self.started: list[str] = []

    def app_start(self, package: str):
        self.started.append(package)

    def current_app(self):
        return {"package": "com.tblenovo.center", "activity": ".Main"}


class _Detector:
    def __init__(self, healthy_sequence: list[bool], events: list[str]):
        self._healthy_sequence = healthy_sequence
        self._idx = 0
        self._events = events

    class _Result:
        def __init__(self, healthy: bool):
            self.is_healthy = healthy

        def to_dict(self):
            if self.is_healthy:
                return {"is_healthy": True, "anomalies": []}
            return {
                "is_healthy": False,
                "anomalies": [{"type": "process_lost", "severity": "high"}],
            }

    def detect(self, package: str, check_baseline: bool = False, **kwargs):
        self._events.append("detect")
        if self._idx >= len(self._healthy_sequence):
            healthy = True
        else:
            healthy = self._healthy_sequence[self._idx]
            self._idx += 1
        return self._Result(healthy)


class _Context:
    def __init__(self, detector):
        self.device = _Device()
        self.anomaly_detector = detector
        self.state_machine = None
        self.baseline_store = None


def test_health_check_runs_before_next_step(monkeypatch):
    monkeypatch.setattr("core.replay_runner.time.sleep", lambda *_: None)
    events: list[str] = []
    detector = _Detector([True], events)
    runner = ReplayRunner(_Context(detector), report_builder=None, check_baseline=False)

    def fake_execute(step):
        events.append(f"step:{step.get('intent')}")
        return True, "ok"

    runner._execute_step = fake_execute  # type: ignore[method-assign]
    result = runner.run_case(
        {
            "name": "timing",
            "app_package": "com.tblenovo.center",
            "steps": [
                {"intent": "step1", "type": "wait", "seconds": 0},
                {"intent": "step2", "type": "wait", "seconds": 0},
            ],
        }
    )
    assert result["status"] == "success"
    assert events == ["step:step1", "detect", "step:step2"]


def test_health_check_failure_blocks_next_step(monkeypatch):
    monkeypatch.setattr("core.replay_runner.time.sleep", lambda *_: None)
    events: list[str] = []
    detector = _Detector([False], events)
    runner = ReplayRunner(_Context(detector), report_builder=None, check_baseline=False)

    def fake_execute(step):
        events.append(f"step:{step.get('intent')}")
        return True, "ok"

    runner._execute_step = fake_execute  # type: ignore[method-assign]
    result = runner.run_case(
        {
            "name": "timing-fail",
            "app_package": "com.tblenovo.center",
            "steps": [
                {"intent": "step1", "type": "wait", "seconds": 0},
                {"intent": "step2", "type": "wait", "seconds": 0},
            ],
        }
    )
    assert result["status"] == "fail"
    assert events == ["step:step1", "detect"]

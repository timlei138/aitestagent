from PIL import Image

from core.anomaly_detector import AnomalyDetector


class MockDevice:
    def __init__(self, hierarchy_xml: str):
        self._hierarchy_xml = hierarchy_xml

    def screenshot(self):
        return Image.new("RGB", (120, 200), color=(255, 255, 255))

    def dump_hierarchy(self):
        return self._hierarchy_xml

    def current_app(self):
        return {"package": "com.tblenovo.center", "activity": ".Main"}


class SequenceCurrentAppDevice(MockDevice):
    def __init__(self, hierarchy_xml: str, sequence: list[dict[str, str]]):
        super().__init__(hierarchy_xml)
        self._sequence = sequence
        self._idx = 0

    def current_app(self):
        if self._idx >= len(self._sequence):
            return self._sequence[-1]
        value = self._sequence[self._idx]
        self._idx += 1
        return value


def test_white_screen_skipped_when_ui_elements_exist():
    device = MockDevice(
        "<hierarchy><node text='问题反馈' clickable='true' class='android.widget.TextView'/></hierarchy>"
    )
    detector = AnomalyDetector(device=device, baseline_store=None, config=None)
    result = detector.detect("com.tblenovo.center", check_baseline=False)
    assert result.is_healthy is True
    assert result.anomalies == []


def test_white_screen_reported_when_no_ui_elements():
    device = MockDevice("<hierarchy><node class='android.view.View'/></hierarchy>")
    detector = AnomalyDetector(device=device, baseline_store=None, config=None)
    result = detector.detect("com.tblenovo.center", check_baseline=False)
    anomaly_types = [item.type.value for item in result.anomalies]
    assert "white_screen" in anomaly_types


def test_process_lost_skipped_when_foreground_recovers():
    device = SequenceCurrentAppDevice(
        "<hierarchy><node text='反馈记录' clickable='true' class='android.widget.TextView'/></hierarchy>",
        [
            {"package": "com.zui.notes", "activity": ".Main"},
            {"package": "com.tblenovo.center", "activity": ".Feedback"},
        ],
    )
    detector = AnomalyDetector(device=device, baseline_store=None, config=None)
    result = detector.detect("com.tblenovo.center", check_baseline=False)
    assert result.is_healthy is True
    assert all(item.type.value != "process_lost" for item in result.anomalies)


def test_process_lost_reported_when_foreground_stays_other_app():
    device = SequenceCurrentAppDevice(
        "<hierarchy><node text='反馈记录' clickable='true' class='android.widget.TextView'/></hierarchy>",
        [
            {"package": "com.zui.notes", "activity": ".Main"},
            {"package": "com.zui.notes", "activity": ".Main"},
        ],
    )
    detector = AnomalyDetector(device=device, baseline_store=None, config=None)
    result = detector.detect("com.tblenovo.center", check_baseline=False)
    types = [item.type.value for item in result.anomalies]
    assert "process_lost" in types


def test_process_lost_skipped_when_expected_package_from_current_app():
    device = SequenceCurrentAppDevice(
        "<hierarchy><node text='反馈记录' clickable='true' class='android.widget.TextView'/></hierarchy>",
        [
            {"package": "com.zui.notes", "activity": ".Main"},
            {"package": "com.zui.notes", "activity": ".Main"},
        ],
    )
    detector = AnomalyDetector(device=device, baseline_store=None, config=None)
    result = detector.detect("", check_baseline=False)
    assert all(item.type.value != "process_lost" for item in result.anomalies)


def test_process_lost_skipped_when_hierarchy_matches_expected():
    device = SequenceCurrentAppDevice(
        "<hierarchy><node package='com.android.settings' text='Settings' clickable='true' class='android.widget.TextView'/></hierarchy>",
        [
            {"package": "com.tblenovo.center", "activity": ".SplashActivity"},
            {"package": "com.tblenovo.center", "activity": ".SplashActivity"},
        ],
    )
    detector = AnomalyDetector(device=device, baseline_store=None, config=None)
    result = detector.detect("com.android.settings", check_baseline=False)
    assert all(item.type.value != "process_lost" for item in result.anomalies)

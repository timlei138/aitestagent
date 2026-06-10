from PIL import Image

from core.semantic_scanner import SemanticScanner
from core.smart_perceiver import PageUnderstanding, UIElement


class MockDevice:
    def __init__(self):
        self.package = "com.android.settings"
        self.activity = ".Main"

    def app_start(self, package):
        self.package = package
        self.activity = ".Main"

    def current_app(self):
        return {"package": self.package, "activity": self.activity}

    def dump_hierarchy(self):
        return "<hierarchy><node text='Wi-Fi' clickable='true' bounds='[0,0][100,60]'/></hierarchy>"

    def screenshot(self):
        return Image.new("RGB", (120, 200), color=(10, 10, 10))

    def click_text(self, text, timeout=0.5):
        if text == "Wi-Fi":
            self.activity = ".WifiSettings"
            return True
        return False

    def click_resource_id(self, rid, timeout=0.5):
        return False

    def click_bounds(self, bounds):
        self.activity = ".ClickedByBounds"

    def press(self, key):
        if key == "back":
            self.activity = ".Main"


class MockPerceiver:
    def __init__(self, device):
        self.device = device

    def screen_signature(self):
        return f"{self.device.package}|{self.device.activity}"

    def perceive(self):
        if self.device.activity == ".Main":
            paths = [
                UIElement(
                    text="Wi-Fi",
                    clickable=True,
                    role="navigation_item",
                    region="left_navigation",
                    priority=1,
                    bounds=(0, 0, 100, 60),
                )
            ]
        else:
            paths = []
        return PageUnderstanding(
            layout="two_pane",
            summary="mock page",
            package=self.device.package,
            activity=self.device.activity,
            width=120,
            height=200,
            regions=[],
            elements=paths,
            primary_paths=paths,
            risky_actions=[],
        )


class MockBaselineStore:
    def __init__(self):
        self.saved = []

    def save_page(self, **kwargs):
        self.saved.append(kwargs)
        return kwargs


class MockDetector:
    class Result:
        is_healthy = True
        has_critical = False

        def to_dict(self):
            return {"is_healthy": True, "anomalies": []}

    def detect(self, *args, **kwargs):
        return self.Result()


def test_semantic_scanner_collects_pages():
    device = MockDevice()
    perceiver = MockPerceiver(device)
    store = MockBaselineStore()
    scanner = SemanticScanner(
        device=device,
        perceiver=perceiver,
        baseline_store=store,
        anomaly_detector=MockDetector(),
        max_depth=2,
        max_pages=5,
        max_clicks=5,
        click_wait=0,
        back_wait=0,
    )
    result = scanner.scan("com.android.settings", "Settings")
    assert result.total_pages >= 1
    assert len(store.saved) >= 1

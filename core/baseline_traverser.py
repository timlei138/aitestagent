from __future__ import annotations

from core.semantic_scanner import ScanNode as TraversalNode
from core.semantic_scanner import ScanResult as TraversalResult
from core.semantic_scanner import SemanticScanner


class BaselineTraverser(SemanticScanner):
    """兼容原文档命名的基线遍历器。

    支持两种构造方式：

    1. **新式**（与 SemanticScanner 一致）::
        BaselineTraverser(device=..., perceiver=..., baseline_store=..., ...)

    2. **兼容旧文档**（自动创建 SmartPerceiver）::
        BaselineTraverser(device, baseline_store, anomaly_detector=None,
                          max_depth=5, max_pages=50, ...)
    """

    def __init__(
        self,
        device=None,
        baseline_store=None,
        anomaly_detector=None,
        max_depth=5,
        max_pages=50,
        click_wait=1.5,
        back_wait=1.0,
        **kwargs,
    ):
        # 检测调用方式：如果传了 perceiver 则走新式，否则兼容旧文档
        if "perceiver" in kwargs:
            # 新式调用，透传给 SemanticScanner
            perceiver = kwargs.pop("perceiver")
            super().__init__(
                device=device,
                perceiver=perceiver,
                baseline_store=baseline_store or kwargs.get("baseline_store"),
                anomaly_detector=anomaly_detector,
                max_depth=max_depth,
                max_pages=max_pages,
                click_wait=click_wait,
                back_wait=back_wait,
                **kwargs,
            )
        else:
            # 兼容旧文档构造方式，自动创建 SmartPerceiver
            from core.smart_perceiver import SmartPerceiver, PerceptionMode

            _device = device
            _baseline_store = baseline_store
            _anomaly_detector = anomaly_detector
            perceiver = SmartPerceiver(_device, llm_client=None, mode=PerceptionMode.HYBRID)
            super().__init__(
                device=_device,
                perceiver=perceiver,
                baseline_store=_baseline_store,
                anomaly_detector=_anomaly_detector,
                max_depth=max_depth,
                max_pages=max_pages,
                click_wait=click_wait,
                back_wait=back_wait,
                **kwargs,
            )

    def traverse(self, app_package: str, start_page_name: str = "首页") -> TraversalResult:
        """兼容旧文档的遍历入口。"""
        return self.scan(app_package, start_page_name)

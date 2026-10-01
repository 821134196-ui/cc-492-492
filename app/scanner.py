"""条码设备模拟接口。

真实车间里这里会换成串口/相机 SDK；本地演练用确定性的模拟实现，
可按需返回可读码、不可读（NO_READ）或错码。
"""
from __future__ import annotations

import random
from dataclasses import dataclass

# 模拟设备的固定返回模式（种子脚本/测试可切换）。
MODES = ("readable", "no_read", "wrong_code")


@dataclass
class ScanResult:
    ok: bool
    payload: str
    detail: str

    def as_tuple(self) -> tuple[bool, str]:
        return self.ok, self.payload


class MockBarcodeScanner:
    """模拟条码扫描枪。"""

    def __init__(self, mode: str = "readable", seed: int = 20261001):
        if mode not in MODES:
            raise ValueError(f"未知扫码模式: {mode}")
        self.mode = mode
        self._rng = random.Random(seed)

    def scan(self, expected: str | None = None) -> ScanResult:
        """扫描一次。expected 为该箱应有的订单条码前缀。"""
        if self.mode == "no_read":
            return ScanResult(False, "", "条码不可读：扫描枪未识别到条码（NO_READ）")
        if self.mode == "wrong_code":
            code = f"X-OTHER-{self._rng.randint(1000, 9999)}"
            detail = f"条码与订单不符：扫到 {code}"
            if expected:
                detail += f"，期望前缀 {expected}"
            return ScanResult(False, code, detail)
        payload = f"{expected or 'ORD'}-BOX-{self._rng.randint(100000, 999999)}"
        return ScanResult(True, payload, "条码可读且与订单匹配")

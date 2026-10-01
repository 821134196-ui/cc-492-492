"""车间设备模拟接口。

真实扫码枪通过串口/USB HID 推送读到的条码；这里用内存脚本队列模拟：
测试或设备面板先把"下一次扫描结果"入队，首箱采集时调用 scan() 弹出。
队列为空时模拟设备脱机（抛 SCANNER_OFFLINE），不会凭空读出条码。
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass


class ScannerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ScanResult:
    ok: bool
    content: str | None       # 可读时的条码内容；不可读为 None
    raw: str                  # 设备原始报文


class MockBarcodeScanner:
    """线程安全的模拟扫码枪。"""

    def __init__(self) -> None:
        self._q: deque[ScanResult] = deque()
        self._lock = threading.Lock()
        self.scan_count = 0

    def load_readable(self, content: str) -> None:
        """脚本排程：下一次扫描读到 content。"""
        with self._lock:
            self._q.append(ScanResult(True, content, f"READ:{content}"))

    def load_unreadable(self) -> None:
        """脚本排程：下一次扫描触发红光但无法解码（条码不可读）。"""
        with self._lock:
            self._q.append(ScanResult(False, None, "NOREAD"))

    def load_offline(self) -> None:
        """脚本排程：下一次扫描时设备无响应。"""
        with self._lock:
            self._q.append(None)  # type: ignore[arg-type]

    def scan(self) -> ScanResult:
        with self._lock:
            if not self._q:
                raise ScannerError("SCANNER_OFFLINE", "扫码枪脱机或未排程扫描结果")
            item = self._q.popleft()
        if item is None:
            raise ScannerError("SCANNER_OFFLINE", "扫码枪无响应")
        self.scan_count += 1
        return item

    def pending(self) -> int:
        with self._lock:
            return len(self._q)


# 全车间一台模拟扫码枪
scanner = MockBarcodeScanner()

# -*- coding: utf-8 -*-
"""QYH-GD300 上位机 v0.1 —— 串口通信层（自动连接 / 只读识别）

职责边界（遵循设计文档 7.5「分层不可越」）：
    本模块属于通信层，运行在独立线程中，不引用任何 UI 框架；
    只通过回调向外发布「字节块 / 状态事件 / 统计」，由上层（WebSocket）消费。

安全约束（重要）：
    1. 识别成功前，本模块**绝不向任何串口写数据**，全程只 open + read；
       因此即使同机插着别的设备调试串口（如 COM3），也不会被我们干扰。
    2. 打开串口时 pyserial 会按默认电平置位 DTR/RTS，这是驱动层行为、
       无法完全避免；除此之外不主动操作任何控制线。
    3. v0.1 不提供任何下行发送接口，协议解析留到下一步。

识别判据（无真实设备时的退化判据）：
    对候选口以 115200/8/N/1 打开并静默监听，按 1 秒窗口统计：
      ① 连续 3 个窗口都有数据；
      ② 速率稳定（(max-min)/max <= RATE_TOLERANCE）；
      ③ 存在重复 >= 3 次的 2 字节组合（疑似帧头）。
    同时满足即锁定该口为接收端，并给出：
      · 疑似帧头 = 出现次数最多的 2 字节组合
      · 疑似帧长 = 该组合两次出现之间的字节间隔中位数
"""

from __future__ import annotations

import statistics
import threading
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from typing import Callable, Optional

import serial
import serial.tools.list_ports as list_ports

from service.protocol import CRC_ORDER, LAYOUT_25, crc16_modbus

# ------------------------------------------------------------------ 常量
BAUD = 115200               # 设计文档 3.2 节：115200 / 8 / N / 1，无流控
WINDOW_SECONDS = 1.0        # 统计窗口长度
DWELL_WINDOWS = 6           # 单个候选口最多监听多少个窗口
REQUIRED_ACTIVE = 3         # 锁定所需的连续有数据窗口数
PAIR_MIN_COUNT = 3          # 疑似帧头的最低重复次数
RATE_TOLERANCE = 0.6        # 速率波动上限
RESCAN_INTERVAL = 2.0       # 无可用端口时的重扫间隔
READ_TIMEOUT = 0.1          # 串口读超时（秒）


def _safe_close(port) -> None:
    try:
        port.close()
    except Exception:
        pass


# ------------------------------------------------------------------ 数据结构
@dataclass
class Window:
    """一个统计窗口内采集到的数据。"""

    bytes_count: int = 0
    elapsed: float = 0.0
    buf: bytearray = field(default_factory=bytearray)

    @property
    def rate(self) -> float:
        return self.bytes_count / self.elapsed if self.elapsed > 0 else 0.0


def _pair_stats(windows) -> tuple[Counter, dict]:
    """统计各窗口内的 2 字节组合频次与出现位置。"""
    counter: Counter = Counter()
    index_lists: dict = defaultdict(list)
    for w in windows:
        buf = w.buf
        local: dict = defaultdict(list)
        for i in range(len(buf) - 1):
            pair = (buf[i], buf[i + 1])
            counter[pair] += 1
            local[pair].append(i)
        for pair, idxs in local.items():
            index_lists[pair].append(idxs)
    return counter, index_lists


def _median_interval(index_lists) -> Optional[int]:
    diffs = []
    for idxs in index_lists:
        for i in range(len(idxs) - 1):
            diffs.append(idxs[i + 1] - idxs[i])
    if not diffs:
        return None
    return int(statistics.median(diffs))


# ------------------------------------------------------------------ 模拟数据源
class MockPort:
    """内置模拟串口：按固定周期吐数据，供无硬件自测。

    两种形态（与上位机的帧格式配置对应）：
      · 二进制（text_mode=False）：严格按设计文档 3.3 上行帧，多字节字段一律
        大端序，CRC-16/MODBUS 覆盖偏移 0–22，CRC 字段本身低位在前（CRC_ORDER）。
        帧长与 CRC 位置直接取自 protocol.LAYOUT_25，不在两处各写一份定义。
      · 文本（text_mode=True）：模拟"固件把数码管数值发出来"的形态，
        即一行一个裸数字 `23150\\r\\n`；数值在 23150 Hz 附近小幅抖动。
    """

    FRAME_LEN = LAYOUT_25.frame_len
    FRAME_PERIOD = 0.1
    TEXT_PERIOD = 0.1

    def __init__(self, name: str = "MOCK0", text_mode: bool = False):
        self.name = name
        self.text_mode = text_mode          # 运行中可由 SerialHub.set_mock_text 切换
        self._last_t = time.monotonic()
        self._seq = 0
        self._pending = bytearray()

    @property
    def in_waiting(self) -> int:
        return len(self._pending)

    def read(self, size: int = 4096) -> bytes:
        time.sleep(0.05)
        now = time.monotonic()
        period = self.TEXT_PERIOD if self.text_mode else self.FRAME_PERIOD
        while now - self._last_t >= period:
            self._last_t += period
            self._pending += self._make_line() if self.text_mode else self._make_frame()
        out = bytes(self._pending[:size])
        del self._pending[:size]
        return out

    def close(self) -> None:
        self._pending.clear()

    def _make_line(self) -> bytes:
        """文本形态：与固件当前输出完全一致——只发一行裸数字（数码管数值）。

        解析层会把裸数字归到 F 字段（频率 Hz），故这里不再加 "F=" 键名。
        """
        self._seq = (self._seq + 1) & 0xFFFF
        freq = 23150 + (self._seq % 37) - 18        # 23132–23168 Hz 小幅抖动
        return f"{freq}\r\n".encode("ascii")

    def _make_frame(self) -> bytes:
        self._seq = (self._seq + 1) & 0xFFFF
        peak = 430 + (self._seq % 41)                 # 峰值：相对刻度值，非 dB
        body = bytearray(LAYOUT_25.crc_offset)
        body[0:2] = b"\xAA\x55"                              # SYNC = 0xAA55
        body[2] = 0x01                                       # VER
        body[3] = 0x01                                       # MSG_TYPE：周期上报
        body[4:6] = (1).to_bytes(2, "big")                   # DEV_ID = 1
        body[6:8] = self._seq.to_bytes(2, "big")             # SEQ
        body[8:12] = int(time.time()).to_bytes(4, "big")     # TIMESTAMP（模拟值）
        body[12] = 0x00                                      # FLAG
        body[13:15] = peak.to_bytes(2, "big")                # PEAK
        body[15:17] = (peak - 12).to_bytes(2, "big")         # AVG
        body[17:19] = (18).to_bytes(2, "big")                # NOISE
        body[19:21] = (self._seq % 97).to_bytes(2, "big")    # PULSE_CNT
        body[21] = 0x7A                                      # BATTERY = 12.2 V
        body[22] = 0x00                                      # RSVD
        crc = crc16_modbus(bytes(body))
        return bytes(body) + crc.to_bytes(2, CRC_ORDER)


# ------------------------------------------------------------------ 串口中枢
class SerialHub:
    """自动发现并锁定接收端串口，向订阅者发布字节流与统计。"""

    def __init__(self, mock: bool = False, mock_text: bool = False):
        self._mock = mock
        self._mock_text = mock_text        # 模拟源形态：文本行 or 二进制定长帧
        self._mock_handle: Optional[MockPort] = None
        self._subscribers: list[Callable[[dict], None]] = []
        self._sub_lock = threading.Lock()

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._state = "searching"          # searching | locked
        self._port: Optional[str] = None
        self._scan: Optional[dict] = None
        self._total_bytes = 0
        self._window_bytes = 0
        self._rate = 0.0
        self._top_pairs: list[dict] = []
        self._frame_head: Optional[str] = None
        self._frame_len: Optional[int] = None
        self._error: Optional[str] = None

    # -------------------------------------------------- 对外接口
    def subscribe(self, callback: Callable[[dict], None]) -> None:
        with self._sub_lock:
            self._subscribers.append(callback)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "type": "status",
                "ts": time.time(),
                "state": self._state,
                "port": self._port,
                "scan": self._scan,
                "total_bytes": self._total_bytes,
                "window_bytes": self._window_bytes,
                "rate": round(self._rate, 1),
                "top_pairs": list(self._top_pairs),
                "frame_head": self._frame_head,
                "frame_len": self._frame_len,
                "mock": self._mock,
                "error": self._error,
            }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="serial-hub", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)
        self._mock_handle = None

    def set_mock_text(self, text: bool) -> None:
        """切换模拟源形态（界面改了帧格式时调用，让 mock 产出对应的字节）。

        真实串口下无副作用：形态由设备固件决定，上位机只解析。
        """
        self._mock_text = bool(text)
        handle = self._mock_handle
        if handle is not None:
            handle.text_mode = self._mock_text

    # -------------------------------------------------- 发布
    def _publish(self, event: dict) -> None:
        with self._sub_lock:
            subs = list(self._subscribers)
        for cb in subs:
            try:
                cb(event)
            except Exception:
                pass

    def _emit_status(self) -> None:
        self._publish(self.snapshot())

    def _event(self, text: str) -> None:
        self._publish({"type": "event", "ts": time.time(), "text": text})

    # -------------------------------------------------- 主循环
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self._state == "locked":
                    self._pump_locked()
                else:
                    self._search_loop()
            except Exception as e:  # 兜底：任何异常都不能让线程退出
                self._set_error(f"通信线程异常: {type(e).__name__}: {e}")
                self._reset_to_search()
        self._reset_to_search()

    def _search_loop(self) -> None:
        ports = self._list_ports()
        if not ports:
            self._set_scan(None)
            if self._stop.wait(RESCAN_INTERVAL):
                return
            return
        for idx, name in enumerate(ports, 1):
            if self._stop.is_set():
                return
            self._set_scan({"index": idx, "total": len(ports), "port": name})
            handle = self._probe(name)
            if handle is not None:
                self._pump_locked(handle)
                return
        if self._stop.wait(RESCAN_INTERVAL):
            return

    # -------------------------------------------------- 识别
    def _probe(self, name: str):
        """监听一个候选口；锁定成功返回已打开的句柄，否则返回 None。"""
        try:
            handle = self._open(name)
        except Exception as e:
            self._set_error(f"打开 {name} 失败: {e}")
            return None

        locked = False
        try:
            windows: list[Window] = []
            for _ in range(DWELL_WINDOWS):
                if self._stop.is_set():
                    return None
                w = self._collect_window(handle, name, WINDOW_SECONDS)
                windows.append(w)
                self._publish_window(windows)
                if self._qualify(windows):
                    self._lock_port(name, windows)
                    locked = True
                    return handle
            return None
        except Exception as e:
            self._set_error(f"{name} 读取异常: {e}")
            return None
        finally:
            if not locked:
                _safe_close(handle)

    def _qualify(self, windows: list[Window]) -> bool:
        if len(windows) < REQUIRED_ACTIVE:
            return False
        recent = windows[-REQUIRED_ACTIVE:]
        if any(w.bytes_count == 0 for w in recent):
            return False
        rates = [w.rate for w in recent]
        top = max(rates)
        if top <= 0:
            return False
        if (top - min(rates)) / top > RATE_TOLERANCE:
            return False
        counter, _ = _pair_stats(recent)
        if not counter:
            return False
        _, count = counter.most_common(1)[0]
        return count >= PAIR_MIN_COUNT

    def _lock_port(self, name: str, windows: list[Window]) -> None:
        recent = windows[-REQUIRED_ACTIVE:]
        counter, index_lists = _pair_stats(recent)
        pair, count = counter.most_common(1)[0]
        frame_head = f"{pair[0]:02X}{pair[1]:02X}"
        frame_len = _median_interval(index_lists.get(pair, []))
        with self._lock:
            self._state = "locked"
            self._port = name
            self._scan = None
            self._frame_head = frame_head
            self._frame_len = frame_len
            self._top_pairs = self._pair_list(counter)
            self._error = None
        self._event(
            f"已锁定接收端 {name}（疑似帧头 {frame_head}"
            + (f"，疑似帧长 {frame_len} 字节" if frame_len else "")
            + f"，帧头重复 {count} 次）"
        )
        self._emit_status()

    # -------------------------------------------------- 锁定后的接收
    def _pump_locked(self, handle) -> None:
        name = self._port
        windows: deque[Window] = deque(maxlen=REQUIRED_ACTIVE)
        try:
            while not self._stop.is_set():
                if not self._port_present(name):
                    self._event(f"接收端 {name} 已移除，回到自动搜索")
                    return
                try:
                    w = self._collect_window(handle, name, WINDOW_SECONDS)
                except Exception as e:
                    self._set_error(f"{name} 读取异常: {e}")
                    self._event(f"接收端 {name} 读取失败，回到自动搜索")
                    return
                windows.append(w)
                self._publish_window(list(windows))
        finally:
            _safe_close(handle)
            self._reset_to_search()

    def _reset_to_search(self) -> None:
        with self._lock:
            if self._state == "searching" and self._port is None and self._frame_head is None:
                return
            self._state = "searching"
            self._port = None
            self._frame_head = None
            self._frame_len = None
            self._rate = 0.0
            self._window_bytes = 0
        self._emit_status()

    # -------------------------------------------------- 采集
    def _collect_window(self, handle, name: str, seconds: float) -> Window:
        w = Window()
        t0 = time.monotonic()
        while True:
            remaining = seconds - (time.monotonic() - t0)
            if remaining <= 0 or self._stop.is_set():
                break
            chunk = handle.read(4096)
            if not chunk:
                continue
            w.buf.extend(chunk)
            w.bytes_count += len(chunk)
            self._publish(
                {
                    "type": "data",
                    "ts": time.time(),
                    "port": name,
                    "n": len(chunk),
                    "hex": chunk.hex().upper(),
                }
            )
        w.elapsed = time.monotonic() - t0
        with self._lock:
            self._total_bytes += w.bytes_count
        return w

    def _publish_window(self, windows: list[Window]) -> None:
        recent = windows[-REQUIRED_ACTIVE:]
        last = recent[-1]
        counter, _ = _pair_stats(recent)
        with self._lock:
            self._window_bytes = last.bytes_count
            self._rate = last.rate
            self._top_pairs = self._pair_list(counter)
        self._emit_status()

    # -------------------------------------------------- 工具
    @staticmethod
    def _pair_list(counter: Counter) -> list[dict]:
        return [
            {"pair": f"{p[0]:02X}{p[1]:02X}", "count": c} for p, c in counter.most_common(3)
        ]

    def _open(self, name: str):
        if self._mock:
            handle = MockPort(name, text_mode=self._mock_text)
            self._mock_handle = handle
            return handle
        # 只读打开：不设置任何写操作，不使用硬件流控
        return serial.Serial(
            name,
            BAUD,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=READ_TIMEOUT,
            write_timeout=READ_TIMEOUT,
            rtscts=False,
            dsrdtr=False,
        )

    def _list_ports(self) -> list[str]:
        if self._mock:
            return ["MOCK0"]
        try:
            ports = list(list_ports.comports())
        except Exception:
            return []
        # USB 转串口（有 VID）优先，主板 legacy 口排在其后
        usb = [p.device for p in ports if p.vid is not None]
        legacy = [p.device for p in ports if p.vid is None]
        return usb + legacy

    def _port_present(self, name: Optional[str]) -> bool:
        if name is None:
            return False
        return name in self._list_ports()

    def _set_scan(self, scan: Optional[dict]) -> None:
        with self._lock:
            self._scan = scan
            self._state = "searching"
            self._port = None
            self._rate = 0.0
            self._window_bytes = 0
            if scan is not None:
                self._error = None
        self._emit_status()

    def _set_error(self, text: str) -> None:
        with self._lock:
            self._error = text
        self._event(text)
        self._emit_status()
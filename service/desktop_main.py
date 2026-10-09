# -*- coding: utf-8 -*-
"""QYH-GD300 故障定位系统 —— 桌面壳入口（单 exe，双击启动）

与 service/main.py 的关系：
    内核完全复用 service.main.create_app()（FastAPI + 现有页面），
    本文件只负责三件事：
      1. 在后台线程启动本地服务（仅绑 127.0.0.1，端口被占用时自动换用空闲端口）
      2. 等服务就绪后，用 pywebview 弹出一个原生桌面窗口加载页面
         （强制使用 WebView2 / edgechromium 内核，保证 CSS Grid 等现代特性正常）
      3. 窗口关闭后干净退出

命令行（主要用于自检与调试）：
    python service/desktop_main.py                 # 弹窗（真实串口，自动识别）
    python service/desktop_main.py --mock          # 弹窗（模拟数据源）
    python service/desktop_main.py --self-check    # 只起服务不开窗，自检后退出
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import uvicorn

from service.main import create_app
from service.layout_config import MODE_TEXT, PROTO_CHOICES
from service.serial_io import SerialHub
from service.settings import (APP_VERSION, DEFAULT_APP_NAME, app_root,
                             normalize_app_name, read_app_name)
from service.ws_push import WsHub

APP_TITLE = DEFAULT_APP_NAME                  # 窗口/弹窗标题的兜底名称
HOST = "127.0.0.1"
PREFERRED_PORT = 8766
STARTUP_TIMEOUT = 20.0
WINDOW_SIZE = (1360, 820)
WINDOW_MIN_SIZE = (1024, 640)


def _window_title() -> str:
    """窗口标题栏文本 = 系统名称 + 版本号（名称可在应用内修改）。"""
    return f"{read_app_name()} {APP_VERSION}"


# ------------------------------------------------------------------ 基础工具
def _log_candidates() -> list[pathlib.Path]:
    """崩溃日志位置：优先 %LOCALAPPDATA%（双击启动时目录未必可写，故留回退）。"""
    candidates: list[pathlib.Path] = []
    base = os.environ.get("LOCALAPPDATA")
    if base:
        candidates.append(pathlib.Path(base) / "QYH-GD300" / "gd300.log")
    candidates.append(app_root() / "gd300.log")
    return candidates


def _install_excepthook(stream) -> None:
    """windowed 构建下崩溃是静默的：必须把 traceback 落到日志或弹窗。"""
    def _hook(exc_type, exc, tb):
        import traceback

        traceback.print_exception(exc_type, exc, tb, file=stream)
        try:
            stream.flush()
        except Exception:
            pass

    sys.excepthook = _hook


def _attach_console() -> None:
    """windowed 打包后没有控制台，sys.stdout 为 None。

    从命令行（如 PowerShell）启动时，附着到父进程控制台，输出可见；
    双击启动时附着失败，退化为写日志文件——否则崩溃时看不到任何原因。
    """
    if sys.platform != "win32" or sys.stdout is not None:
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        if kernel32.AttachConsole(-1):                     # ATTACH_PARENT_PROCESS
            # 必须跟随控制台实际代码页，否则中文输出会是乱码（控制台通常是 cp936）
            encoding = f"cp{kernel32.GetConsoleOutputCP()}"
            kwargs = {"encoding": encoding, "errors": "replace", "buffering": 1}
            sys.stdout = open("CONOUT$", "w", **kwargs)
            sys.stderr = open("CONOUT$", "w", **kwargs)
            return
    except Exception:
        pass

    stream = None
    for path in _log_candidates():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            stream = open(path, "w", encoding="utf-8", errors="replace", buffering=1)
            break
        except Exception:
            stream = None
    if stream is None:
        return
    sys.stdout = stream
    sys.stderr = stream
    _install_excepthook(stream)


def _log(message: str) -> None:
    """无控制台的 windowed 构建下 sys.stdout 为 None，必须容错。"""
    stream = sys.stdout
    if stream is None:
        return
    try:
        print(message)
    except Exception:
        pass


def _alert(message: str) -> None:
    """无控制台时的用户可见错误提示（零依赖，直接调 Win32）。"""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, APP_TITLE, 0x10)  # MB_ICONERROR
        return
    except Exception:
        pass
    _log(message)


def _pick_port(preferred: int) -> int:
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((HOST, candidate))
            except OSError:
                continue
            return sock.getsockname()[1]
    raise RuntimeError(f"无法在 {HOST} 上分配监听端口")


def _start_server(app, port: int):
    config = uvicorn.Config(app, host=HOST, port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()
    return server, thread


def _wait_ready(port: int, timeout: float = STARTUP_TIMEOUT) -> bool:
    url = f"http://{HOST}:{port}/api/status"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.15)
    return False


def _shutdown(server, thread) -> None:
    server.should_exit = True
    thread.join(timeout=5.0)


# ------------------------------------------------------------------ 自检
def _fetch(url: str):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, resp.read()


def _run_self_check(port: int) -> int:
    """打包产物自检：不打开窗口，只验证内核可用（用于自动化验证 exe）。

    输出一律使用 ASCII：控制台代码页在不同机器/不同启动方式下不一致，
    中文会乱码，诊断信息必须做到任何环境下都可读。
    """
    results: list[tuple[str, bool, str]] = []

    try:
        status, body = _fetch(f"http://{HOST}:{port}/api/status")
        data = json.loads(body.decode("utf-8"))
        ok = status == 200 and data.get("state") in ("searching", "locked")
        results.append(("GET /api/status", ok,
                        f"state={data.get('state')} mock={data.get('mock')}"))
    except Exception as e:
        results.append(("GET /api/status", False, f"{type(e).__name__}: {e}"))

    try:
        status, body = _fetch(f"http://{HOST}:{port}/")
        text = body.decode("utf-8")
        ok = status == 200 and "hexPane" in text
        results.append(("GET / (static UI)", ok, f"HTTP {status}, {len(text)} bytes"))
    except Exception as e:
        results.append(("GET / (static UI)", False, f"{type(e).__name__}: {e}"))

    try:
        status, body = _fetch(f"http://{HOST}:{port}/api/settings")
        data = json.loads(body.decode("utf-8"))
        ok = status == 200 and bool(data.get("app_name")) and data.get("max_len") == 32
        results.append(("GET /api/settings", ok,
                        f"app_name={data.get('app_name')} max_len={data.get('max_len')}"))
    except Exception as e:
        results.append(("GET /api/settings", False, f"{type(e).__name__}: {e}"))

    try:
        # 模拟源每 0.1 s 一帧，给解析器最多 3 s 攒够数据（真实串口无帧时会判 FAIL）
        deadline = time.monotonic() + 3.0
        data: dict = {}
        while time.monotonic() < deadline:
            status, body = _fetch(f"http://{HOST}:{port}/api/protocol")
            data = json.loads(body.decode("utf-8"))
            if data.get("frames_ok"):
                break
            time.sleep(0.2)
        ok = (status == 200 and data.get("frames_ok", 0) > 0
              and data.get("crc_err") == 0 and len(data.get("columns") or []) >= 2)
        results.append(("GET /api/protocol", ok,
                        f"kind={data.get('kind')} layout={data.get('layout')} "
                        f"frames_ok={data.get('frames_ok')} "
                        f"err={data.get('crc_err')} resync={data.get('resync')} "
                        f"cols={len(data.get('columns') or [])}"))
    except Exception as e:
        results.append(("GET /api/protocol", False, f"{type(e).__name__}: {e}"))

    try:
        lan_ip = socket.gethostbyname(socket.gethostname())
        if lan_ip.startswith("127."):
            results.append(("bind 127.0.0.1 only", True, "no LAN address, skipped"))
        else:
            with socket.create_connection((lan_ip, port), timeout=2):
                results.append(("bind 127.0.0.1 only", False,
                                f"reachable from {lan_ip}"))
    except OSError:
        results.append(("bind 127.0.0.1 only", True, "not reachable from LAN, as expected"))
    except Exception as e:
        results.append(("bind 127.0.0.1 only", True, f"undetermined ({type(e).__name__})"))

    for name, ok, detail in results:
        _log(f"[{'PASS' if ok else 'FAIL'}] {name} -- {detail}")
    failed = [name for name, ok, _ in results if not ok]
    _log(f"[GD300] self-check: {'ALL PASS' if not failed else 'FAILED -> ' + ', '.join(failed)}")
    return 0 if not failed else 1


# ------------------------------------------------------------------ 原生壳接口
class ShellApi:
    """暴露给页面的原生壳接口（前端通过 window.pywebview.api 调用）。

    页面里改完系统名称后，用它把新名称同步到 Windows 窗口标题栏——
    原生标题栏无法只靠 JS 修改，必须由壳代劳。
    """

    def __init__(self) -> None:
        self._window = None

    def bind(self, window) -> None:
        self._window = window

    def set_app_title(self, name: str) -> str:
        """把系统名称同步到窗口标题栏，返回实际生效的标题（非法名称返回空串）。"""
        try:
            safe = normalize_app_name(name)
        except ValueError:
            return ""
        title = f"{safe} {APP_VERSION}"
        window = self._window
        if window is not None:
            try:
                window.title = title
            except Exception as e:
                _log(f"[GD300] set window title failed: {type(e).__name__}: {e}")
                return ""
        return title


# ------------------------------------------------------------------ 入口
def main() -> int:
    _attach_console()
    _log(f"[GD300] start: {sys.executable} {sys.argv[1:]}")
    parser = argparse.ArgumentParser(description="QYH-GD300 故障定位系统（桌面版）")
    parser.add_argument("--mock", action="store_true", help="启用内置模拟数据源（无需真实设备）")
    parser.add_argument("--self-check", action="store_true",
                        help="只启动本地服务并自检，不打开窗口")
    parser.add_argument("--proto", default=None, choices=sorted(PROTO_CHOICES),
                        help="上行链路：text=ASCII 文本行（设备当前固件）/ "
                             "25=设计文档 3.3 定长帧 / 14=评估报告 7.1 定长帧；"
                             "缺省则用界面里保存的帧格式配置")
    args = parser.parse_args()

    port = _pick_port(PREFERRED_PORT)
    hub = SerialHub(mock=args.mock)
    app = create_app(hub, WsHub(), explicit_layout=args.proto)
    info = app.state.gd300
    layout = info["layout"]
    hub.set_mock_text(layout.key == MODE_TEXT)          # 模拟源与链路保持一致
    server, thread = _start_server(app, port)
    _log(f"[GD300] service starting on {HOST}:{port}")
    if layout.key == MODE_TEXT:
        _log(f"[GD300] uplink: {layout.title} (source={info['source']}, "
             f"line-based KEY=VALUE)")
    else:
        _log(f"[GD300] frame layout: {layout.title} "
             f"(source={info['source']}, frame_len={layout.frame_len}, "
             f"crc_offset={layout.crc_offset}, covers={layout.crc_covers}, "
             f"crc_order={layout.crc_order}, byte_order={layout.byte_order})")

    if not _wait_ready(port):
        _alert(f"本地服务启动失败（端口 {port}）。\n请检查是否有安全软件拦截本程序。")
        _shutdown(server, thread)
        return 2

    if args.self_check:
        code = _run_self_check(port)
        _shutdown(server, thread)
        return code

    _log(f"[GD300] 本地服务已就绪: http://{HOST}:{port}")

    try:
        import webview
    except Exception as e:
        _alert(f"桌面窗口组件加载失败：{e}\n\n本程序需要 Microsoft Edge WebView2 运行时，"
               f"请先安装后重试。\n临时可用浏览器访问 http://{HOST}:{port}")
        _shutdown(server, thread)
        return 3

    _log("[GD300] creating window")
    api = ShellApi()
    window = webview.create_window(
        _window_title(),
        f"http://{HOST}:{port}/",
        width=WINDOW_SIZE[0],
        height=WINDOW_SIZE[1],
        min_size=WINDOW_MIN_SIZE,
        confirm_close=False,
        text_select=True,          # 允许选中复制 HEX 文本
        background_color="#12161F",
        js_api=api,
    )
    api.bind(window)

    try:
        _log("[GD300] webview.start(edgechromium)")
        # 强制 WebView2：IE(mshtml) 内核不支持 CSS Grid/flex gap，界面会散版
        webview.start(gui="edgechromium")
        _log("[GD300] window closed")
    except Exception as e:
        _log(f"[GD300] window failed: {type(e).__name__}: {e}")
        _alert(f"窗口启动失败：{e}\n\n请确认已安装 Microsoft Edge WebView2 运行时。")
        _shutdown(server, thread)
        return 4

    _shutdown(server, thread)
    return 0


if __name__ == "__main__":
    sys.exit(main())
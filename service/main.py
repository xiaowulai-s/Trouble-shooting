# -*- coding: utf-8 -*-
"""QYH-GD300 上位机 v0.1 —— 本地服务入口

启动方式（工作目录任意，路径由脚本自身解析）：
    python service/main.py            # 真实串口：自动识别接收端（只读，不写数据）
    python service/main.py --mock     # 无硬件自测：启用内置模拟数据源
    python service/main.py --mock --port 8766

访问：http://127.0.0.1:8766
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from service.layout_config import (MODE_TEXT, PROTO_CHOICES, describe,
                                   layout_from_config, resolve_layout_choice,
                                   save_config)
from service.protocol import Decoder
from service.protocol_text import TextLineDecoder
from service.serial_io import SerialHub
from service.settings import (APP_VERSION, DEFAULT_APP_NAME, MAX_APP_NAME,
                             read_app_name, save_app_name)
from service.ws_push import WsHub

STATIC_DIR = pathlib.Path(__file__).resolve().parent.parent / "web" / "static"


def _make_decoder(layout) -> object:
    """按生效布局构造解析器：文本链路走 TextLineDecoder，其余走二进制 Decoder。

    两条链路对外接口一致（feed / snapshot / layout），上层不必区分。
    """
    if layout.key == MODE_TEXT:
        return TextLineDecoder()
    return Decoder(layout)


def create_app(hub: SerialHub, ws_hub: WsHub, explicit_layout: str | None = None) -> FastAPI:
    """组装服务。

    explicit_layout：命令行 --proto 的值（None 表示听 settings.json 的配置）。
    命令行是"临时试一下"，只影响本次运行，不写配置文件。
    """
    layout, cfg, source = resolve_layout_choice(explicit_layout)
    state = {"cfg": cfg, "source": source, "decoder": _make_decoder(layout)}

    def _layout_view() -> dict:
        """界面用的帧格式快照；命令行覆盖时不误导界面显示已存的配置。"""
        current = state["decoder"].layout
        shown = state["cfg"] if state["source"] == "config" else {**state["cfg"], "mode": current.key}
        return describe(current, shown, state["source"])

    def _on_hub_message(message: dict) -> None:
        """通信层字节流 → 协议层切帧 → 推给页面（在串口线程里被调用）。"""
        if message.get("type") != "data":
            return
        try:
            chunk = bytes.fromhex(message.get("hex") or "")
        except ValueError:
            return
        decoder = state["decoder"]
        frames = decoder.feed(chunk, ts=message.get("ts"))
        if not frames:
            return
        ws_hub.broadcast({
            "type": "frames",
            "items": [f.to_payload() for f in frames],
            "stats": decoder.snapshot(),
        })

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        ws_hub.bind_loop(asyncio.get_running_loop())
        hub.subscribe(ws_hub.broadcast)
        hub.subscribe(_on_hub_message)
        hub.start()
        try:
            yield
        finally:
            hub.stop()

    app = FastAPI(title=f"{read_app_name()} {APP_VERSION}", lifespan=lifespan)
    # 桌面壳要打印"当前帧格式来源"，把生效结果挂在 app 上供其读取
    app.state.gd300 = {"layout": layout, "cfg": cfg, "source": source, "state": state}

    @app.get("/api/status")
    async def api_status():
        return JSONResponse(hub.snapshot())

    @app.get("/api/protocol")
    async def api_protocol():
        """协议解析统计 + 最近若干结构化帧（含表格列定义）。"""
        return JSONResponse(state["decoder"].snapshot(recent=50))

    @app.get("/api/settings")
    async def api_get_settings():
        return JSONResponse({
            "app_name": read_app_name(),
            "default_name": DEFAULT_APP_NAME,
            "version": APP_VERSION,
            "max_len": MAX_APP_NAME,
            "layout": _layout_view(),
        })

    @app.post("/api/settings")
    async def api_set_settings(payload: dict):
        """保存系统名称 / 帧格式；帧格式变更时运行中重切布局（清统计并通知界面）。"""
        body = payload if isinstance(payload, dict) else {}
        name = read_app_name()
        if "app_name" in body:
            try:
                name = save_app_name(body.get("app_name"))
            except ValueError as e:             # 名称非法（空值等）
                return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
            except OSError as e:                # 位置都写不了
                return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

        if "layout" in body:
            try:
                new_cfg = save_config(body.get("layout"))
                new_layout = layout_from_config(new_cfg)    # 参数非法在此抛
            except ValueError as e:
                return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
            except OSError as e:
                return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
            state["cfg"] = new_cfg
            state["source"] = "config"
            decoder = state["decoder"]
            if new_layout.key == MODE_TEXT or decoder.layout.key == MODE_TEXT:
                # 文本链路 ↔ 二进制链路：解析器类型不同，必须换实例（清空旧统计）
                state["decoder"] = _make_decoder(new_layout)
                ws_hub.broadcast({"type": "layout", "stats": state["decoder"].snapshot(),
                                  "text": f"帧格式已切换：{new_layout.title}"})
            elif new_layout != decoder.layout:
                stats = decoder.set_layout(new_layout)
                ws_hub.broadcast({"type": "layout", "stats": stats,
                                  "text": f"帧格式已切换：{new_layout.title}"})
            hub.set_mock_text(new_layout.key == MODE_TEXT)   # 模拟源跟着换形态

        return JSONResponse({"ok": True, "app_name": name, "layout": _layout_view()})

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket):
        await websocket.accept()
        queue = ws_hub.register()
        try:
            while True:
                message = await queue.get()
                await websocket.send_json(message)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            ws_hub.unregister(queue)

    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="QYH-GD300 上位机 v0.1")
    parser.add_argument("--mock", action="store_true",
                        help="启用内置模拟数据源（无需真实设备）")
    parser.add_argument("--proto", default=None, choices=sorted(PROTO_CHOICES),
                        help="上行链路：text=ASCII 文本行（设备当前固件）/ "
                             "25=设计文档 3.3 定长帧 / 14=评估报告 7.1 定长帧；"
                             "缺省则用界面里保存的帧格式配置")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    parser.add_argument("--port", type=int, default=8766, help="监听端口")
    args = parser.parse_args()

    hub = SerialHub(mock=args.mock)
    ws_hub = WsHub()
    app = create_app(hub, ws_hub, explicit_layout=args.proto)
    info = app.state.gd300
    layout = info["layout"]
    source = "命令行 --proto" if info["source"] == "cli" else "界面配置"
    hub.set_mock_text(layout.key == MODE_TEXT)          # 模拟源与链路保持一致

    mode = "模拟数据源" if args.mock else "真实串口（自动识别，识别前只读不写）"
    detail = ("按 \\r\\n 切行、解析裸数字或 KEY=VALUE（如 23150 / F=23150）"
              if layout.key == MODE_TEXT
              else f"CRC 覆盖偏移 0–{layout.crc_covers - 1}")
    print(f"[GD300] 数据源: {mode}")
    print(f"[GD300] 上行链路: {layout.title}（来源：{source}），{detail}")
    print(f"[GD300] 请打开 http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
# -*- coding: utf-8 -*-
"""QYH-GD300 上位机 v0.1 —— 本地服务入口

启动方式（工作目录任意，路径由脚本自身解析）：
    python service/main.py            # 真实串口：自动识别接收端（只读，不写数据）
    python service/main.py --mock     # 无硬件自测：启用内置模拟数据源
    python service/main.py --mock --port 8766

访问：http://127.0.0.1:8766

数据流水线（第一批）：
    串口字节 → 协议层切帧（protocol / protocol_text）
             → 分析层按"当前方案"的字段表取值并线性换算（analysis.Analyzer）
             → 推给页面（表格）+ 写入会话缓冲（store.SampleStore，退出即清）
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import datetime
import io
import pathlib
import sys
import time
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from service import schema as scheme_store
from service.analysis import COL_TS, Analyzer, format_number
from service.layout_config import (MODE_TEXT, PROTO_CHOICES, cfg_from_scheme,
                                   describe, layout_from_config,
                                   layout_from_scheme, resolve_layout_choice,
                                   save_config)
from service.protocol import Decoder, _ts_text
from service.protocol_text import TextLineDecoder
from service.serial_io import SerialHub
from service.settings import (APP_VERSION, DEFAULT_APP_NAME, MAX_APP_NAME,
                             read_app_name, save_app_name)
from service.store import SampleStore
from service.ws_push import WsHub

STATIC_DIR = pathlib.Path(__file__).resolve().parent.parent / "web" / "static"


def _make_decoder(layout) -> object:
    """按生效布局构造解析器：文本链路走 TextLineDecoder，其余走二进制 Decoder。

    两条链路对外接口一致（feed / snapshot / layout），上层不必区分。
    """
    if layout.key == MODE_TEXT:
        return TextLineDecoder()
    return Decoder(layout)


def _raw_bytes(frame) -> bytes | None:
    """二进制帧的整帧字节（按偏移取值用）；文本帧没有 raw_hex，返回 None。"""
    raw_hex = getattr(frame, "raw_hex", None)
    if not raw_hex:
        return None
    try:
        return bytes.fromhex(str(raw_hex).replace(" ", ""))
    except ValueError:
        return None


def create_app(hub: SerialHub, ws_hub: WsHub, explicit_layout: str | None = None) -> FastAPI:
    """组装服务。

    explicit_layout：命令行 --proto 的值（None 表示听 settings.json / 方案的配置）。
    命令行是"临时试一下"，只影响本次运行，不写配置文件。
    """
    scheme = scheme_store.load_active_scheme()
    if explicit_layout:
        layout = resolve_layout_choice(explicit_layout)[0]
        source = "cli"
        # 命令行强制换了链路时，字段表也必须跟着换，否则取值来源对不上
        if (layout.key == MODE_TEXT) != (scheme.frame.get("kind") == "text"):
            scheme = scheme_store.builtin_for_layout(layout.key)
    else:
        layout = layout_from_scheme(scheme)
        source = "config"

    cfg = cfg_from_scheme(scheme)
    store = SampleStore(flush_ms=scheme.storage.get("flush_ms", 500))
    state = {
        "cfg": cfg,
        "source": source,
        "decoder": _make_decoder(layout),
        "scheme": scheme,
        "analyzer": Analyzer(scheme.fields),
        "store": store,
    }

    def _stats_view() -> dict:
        """协议统计 + 方案口径的列名（表格列随字段表变化）。"""
        stats = state["decoder"].snapshot()
        stats["columns"] = state["analyzer"].columns()
        stats["scheme"] = state["scheme"].name
        return stats

    def _field_meta() -> dict[str, dict]:
        """字段 id → {name, unit, decimals}（导出表头、报警列表补名用）。"""
        out: dict[str, dict] = {}
        for f in state["analyzer"].fields:
            out[f.id] = {
                "name": str(getattr(f, "name", "") or f.id),
                "unit": str(getattr(f, "unit", "") or ""),
                "decimals": int(getattr(f, "decimals", 0) or 0),
            }
        return out

    def _csv_response(text: str, prefix: str) -> Response:
        """带 BOM 的 UTF-8 CSV 附件响应（BOM 让 Excel 正确识别中文）。"""
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        return Response(
            content=("\ufeff" + text).encode("utf-8"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{prefix}_{stamp}.csv"'},
        )

    def _layout_view() -> dict:
        """界面用的帧格式快照；命令行覆盖时不误导界面显示已存的配置。"""
        current = state["decoder"].layout
        shown = state["cfg"] if state["source"] == "config" else {**state["cfg"], "mode": current.key}
        return describe(current, shown, state["source"])

    def _on_hub_message(message: dict) -> None:
        """通信层字节流 → 协议层切帧 → 分析层换算 → 推给页面（在串口线程里被调用）。"""
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

        analyzer = state["analyzer"]
        sample_store = state["store"]
        items: list[dict] = []
        alarms: list[dict] = []
        for frame in frames:
            payload = frame.to_payload()
            ts = payload.get("ts") or time.time()
            result = analyzer.process(ts, values=getattr(frame, "values", None),
                                      raw=_raw_bytes(frame))
            payload["fields"] = result["fields"]
            payload["cells"] = result["cells"]
            items.append(payload)
            if sample_store is not None:
                sample_store.append(ts, [(fid, meta["value"])
                                         for fid, meta in result["fields"].items()])
                # 报警状态跳变：入库（长期留存靠导出）+ 稍后推送界面
                for ev in result.get("alarms", []):
                    # kind 编码进入/解除：side 表示阈值侧，"<side>_clear" 表示解除
                    kind = ev["side"] if ev["active"] else f"{ev['side']}_clear"
                    sample_store.append_alarm(ts, ev["field"], kind, ev["value"],
                                              ev["limit"], ev["text"])
                    alarms.append({**ev, "ts": ts})

        ws_hub.broadcast({"type": "frames", "items": items, "stats": _stats_view()})
        if alarms:
            ws_hub.broadcast({"type": "alarm", "items": alarms})

    def _apply_scheme(scheme_obj, note: str) -> dict:
        """把方案切为当前生效方案：重建解析器与分析器，并通知界面（串口线程安全）。"""
        new_layout = layout_from_scheme(scheme_obj)      # 常数非法在此抛 ValueError
        state["scheme"] = scheme_obj
        state["analyzer"].set_fields(scheme_obj.fields)
        state["cfg"] = cfg_from_scheme(scheme_obj)
        state["source"] = "config"
        state["decoder"] = _make_decoder(new_layout)     # 换实例：统计清零、rev 归零
        hub.set_mock_text(new_layout.key == MODE_TEXT)   # 模拟源跟着换形态
        ws_hub.broadcast({"type": "layout", "stats": _stats_view(), "text": note})
        return _stats_view()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        store.start()                                     # 会话缓冲：失败则降级为不落盘
        ws_hub.bind_loop(asyncio.get_running_loop())
        hub.subscribe(ws_hub.broadcast)
        hub.subscribe(_on_hub_message)
        hub.start()
        try:
            yield
        finally:
            hub.stop()
            store.close_and_clear()                       # 退出即清

    app = FastAPI(title=f"{read_app_name()} {APP_VERSION}", lifespan=lifespan)
    # 桌面壳要打印"当前帧格式来源"，把生效结果挂在 app 上供其读取
    app.state.gd300 = {"layout": layout, "cfg": cfg, "source": source, "state": state}

    @app.get("/api/status")
    async def api_status():
        return JSONResponse(hub.snapshot())

    @app.get("/api/protocol")
    async def api_protocol():
        """协议解析统计 + 最近若干结构化帧（含方案口径的表格列定义）。"""
        stats = state["decoder"].snapshot(recent=50)
        stats["columns"] = state["analyzer"].columns()
        stats["scheme"] = state["scheme"].name
        return JSONResponse(stats)

    @app.get("/api/fields")
    async def api_fields():
        """当前方案的字段表快照（供字段表编辑器与曲线图例使用）。"""
        return JSONResponse({
            "scheme": state["scheme"].name,
            "columns": state["analyzer"].columns(),
            "fields": scheme_store.fields_view(state["scheme"]),
        })

    @app.get("/api/store")
    async def api_store(t0: float | None = None, t1: float | None = None,
                        fields: str | None = None, limit: int = 5000):
        """查询当前会话缓冲（按时间区间/字段）。退出即清，不能当长期存储。"""
        ids = [x for x in str(fields or "").split(",") if x] or None
        return JSONResponse({
            "stats": state["store"].stats(),
            "items": state["store"].query_range(t0, t1, ids, limit=limit),
        })

    # -------------------------------------------------- 报警 / 导出 / 下行
    @app.get("/api/alarms")
    async def api_alarms(limit: int = 500):
        """当前会话的报警记录（含进入与解除），供界面报警列表与导出。"""
        rows = state["store"].query_alarms(limit=limit)
        meta = _field_meta()
        for r in rows:
            info = meta.get(r["field"], {})
            r["name"] = info.get("name", r["field"])
            r["unit"] = info.get("unit", "")
            r["decimals"] = info.get("decimals", 0)
            r["ts_text"] = _ts_text(r["ts"])
            kind = str(r.get("kind") or "")
            r["active"] = not kind.endswith("_clear")     # 进入=True / 解除=False
            r["side"] = kind[:-len("_clear")] if kind.endswith("_clear") else kind
        return JSONResponse({"items": rows, "stats": state["store"].stats()})

    @app.get("/api/export/samples.csv")
    async def api_export_samples(t0: float | None = None, t1: float | None = None,
                                 fields: str | None = None):
        """采样数据导出的 CSV（长表→宽表：一行一个时刻，一列一个字段）。"""
        ids = [x for x in str(fields or "").split(",") if x] or None
        rows = state["store"].query_range(t0, t1, ids, limit=200000)
        meta = _field_meta()
        order = [f.id for f in state["analyzer"].fields
                 if (not ids or f.id in ids)]
        known = set(order)
        for r in rows:                                  # 兜底：库里出现字段表外的字段也补列
            if r["field"] not in known:
                known.add(r["field"])
                order.append(r["field"])

        def label(fid: str) -> str:
            info = meta.get(fid)
            if not info:
                return fid
            return f"{info['name']}({info['unit']})" if info["unit"] else info["name"]

        by_ts: dict[float, dict[str, Any]] = {}
        for r in rows:
            by_ts.setdefault(r["ts"], {})[r["field"]] = r["value"]

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([COL_TS] + [label(fid) for fid in order])
        for ts in sorted(by_ts):
            values = by_ts[ts]
            line = [_ts_text(ts)]
            for fid in order:
                v = values.get(fid)
                line.append("" if v is None
                            else format_number(v, meta.get(fid, {}).get("decimals", 0)))
            writer.writerow(line)
        return _csv_response(buf.getvalue(), "samples")

    @app.get("/api/export/alarms.csv")
    async def api_export_alarms():
        """报警记录导出的 CSV（按时间升序）。"""
        rows = state["store"].query_alarms(limit=100000)
        meta = _field_meta()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["时间", "字段ID", "字段名", "类型", "状态", "数值", "限值", "说明"])
        for r in reversed(rows):                        # query_alarms 是倒序，导出改升序
            info = meta.get(r["field"], {})
            kind = str(r.get("kind") or "")
            clear = kind.endswith("_clear")
            side = kind[:-len("_clear")] if clear else kind
            writer.writerow([
                _ts_text(r["ts"]), r["field"],
                info.get("name", r["field"]),
                "上限" if side == "high" else ("下限" if side == "low" else side),
                "解除" if clear else "进入",
                "" if r["value"] is None else format_number(r["value"], info.get("decimals", 0)),
                "" if r["limit"] is None else format_number(r["limit"], info.get("decimals", 0)),
                r["text"] or "",
            ])
        return _csv_response(buf.getvalue(), "alarms")

    @app.post("/api/send")
    async def api_send(payload: dict):
        """手动下行：仅在接收端已锁定时允许（扫描期只读，禁止写串口）。"""
        body = payload if isinstance(payload, dict) else {}
        mode = str(body.get("mode", "text")).strip().lower()
        raw_text = str(body.get("data", ""))
        if mode == "hex":
            compact = "".join(raw_text.split())
            try:
                data = bytes.fromhex(compact)
            except ValueError:
                return JSONResponse({"ok": False, "error": "HEX 格式非法（只允许 0–9 A–F）"},
                                    status_code=400)
        else:
            data = raw_text.encode("utf-8", "replace")
            if body.get("append_newline", True):
                data += b"\n"
        if not data:
            return JSONResponse({"ok": False, "error": "发送内容为空"}, status_code=400)
        if len(data) > 512:
            return JSONResponse({"ok": False, "error": "单次下行最多 512 字节"},
                                status_code=400)
        ok, message = hub.send(data)
        if not ok:
            return JSONResponse({"ok": False, "error": message}, status_code=409)
        return JSONResponse({"ok": True, "bytes": len(data), "mode": mode,
                             "hex": data.hex().upper(), "message": message})

    # -------------------------------------------------- 方案 CRUD
    @app.get("/api/schemes")
    async def api_schemes():
        return JSONResponse({"active": state["scheme"].name,
                             "items": scheme_store.list_schemes()})

    @app.get("/api/schemes/{name}")
    async def api_scheme_get(name: str):
        try:
            scheme_obj = scheme_store.load_scheme(name)
        except KeyError:
            return JSONResponse({"ok": False, "error": f"方案不存在：{name}"}, status_code=404)
        return JSONResponse({"ok": True, "scheme": scheme_store.scheme_to_dict(scheme_obj)})

    @app.post("/api/schemes")
    async def api_scheme_save(payload: dict):
        """保存（新增/覆盖）一个自定义方案；内置方案只读。"""
        body = payload if isinstance(payload, dict) else {}
        raw = body.get("scheme") if isinstance(body.get("scheme"), dict) else body
        try:
            saved = scheme_store.save_scheme(raw)
        except ValueError as e:                     # 校验不过 / 企图覆盖内置
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except OSError as e:                        # 配置位置都写不了
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        return JSONResponse({"ok": True, "scheme": scheme_store.scheme_to_dict(saved)})

    @app.delete("/api/schemes/{name}")
    async def api_scheme_delete(name: str):
        try:
            scheme_store.delete_scheme(name)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except KeyError:
            return JSONResponse({"ok": False, "error": f"方案不存在：{name}"}, status_code=404)
        except OSError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        return JSONResponse({"ok": True, "active": state["scheme"].name})

    @app.post("/api/schemes/{name}/activate")
    async def api_scheme_activate(name: str):
        """切换当前方案：立即生效（重建解析器/分析器，清表并通知界面）。"""
        try:
            scheme_obj = scheme_store.load_scheme(name)
        except KeyError:
            return JSONResponse({"ok": False, "error": f"方案不存在：{name}"}, status_code=404)
        try:
            scheme_store.activate_scheme(scheme_obj.name)     # 记住激活方案名
            _apply_scheme(scheme_obj, f"已切换方案：{scheme_obj.name}")
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except OSError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        return JSONResponse({"ok": True, "active": scheme_obj.name,
                             "scheme": scheme_store.scheme_to_dict(scheme_obj)})

    @app.get("/api/settings")
    async def api_get_settings():
        return JSONResponse({
            "app_name": read_app_name(),
            "default_name": DEFAULT_APP_NAME,
            "version": APP_VERSION,
            "max_len": MAX_APP_NAME,
            "layout": _layout_view(),
            "scheme": state["scheme"].name,
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
                ws_hub.broadcast({"type": "layout", "stats": _stats_view(),
                                  "text": f"帧格式已切换：{new_layout.title}"})
            elif new_layout != decoder.layout:
                decoder.set_layout(new_layout)
                ws_hub.broadcast({"type": "layout", "stats": _stats_view(),
                                  "text": f"帧格式已切换：{new_layout.title}"})
            # 链路类型变了，字段表口径也要跟着换（否则取值来源对不上）
            if (new_layout.key == MODE_TEXT) != (state["scheme"].frame.get("kind") == "text"):
                state["scheme"] = scheme_store.builtin_for_layout(new_layout.key)
                state["analyzer"].set_fields(state["scheme"].fields)
                ws_hub.broadcast({"type": "layout", "stats": _stats_view(),
                                  "text": f"字段表已随链路切到：{state['scheme'].name}"})
            hub.set_mock_text(new_layout.key == MODE_TEXT)   # 模拟源跟着换形态

        return JSONResponse({"ok": True, "app_name": name, "layout": _layout_view(),
                             "scheme": state["scheme"].name})

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
                             "缺省则用当前激活方案")
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
    detail = ("按 \\n 切行、解析裸数字或 KEY=VALUE（如 23150 / F=23150）"
              if layout.key == MODE_TEXT
              else f"CRC 覆盖偏移 0–{layout.crc_covers - 1}")
    print(f"[GD300] 数据源: {mode}")
    print(f"[GD300] 上行链路: {layout.title}（来源：{source}），{detail}")
    print(f"[GD300] 当前方案: {info['state']['scheme'].name}")
    print(f"[GD300] 请打开 http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
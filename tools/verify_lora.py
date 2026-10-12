# -*- coding: utf-8 -*-
"""第三批验证脚本：LoRa 主从通用帧只读监听（解析器单测 + mock 服务端到端）

用法：
    python service/main.py --mock --port 8793 --proto lora   # 另开一个终端
    python tools/verify_lora.py

两部分：
    A. 解析器单测（不依赖服务）：正确帧 / 噪声重同步 / CRC 错帧 / 分片喂入 /
       载荷内 AA55 / 广播与上下行方向 / 未知命令码
    B. API 端到端（对运行中的 mock 服务）：状态锁定 / 协议统计 / 内置方案 /
       字段表 / 帧格式快照 / 激活方案切换 / 导出 CSV
"""
import json
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from service.protocol_lora import LoraDecoder, build_frame, cmd_name  # noqa: E402

API_BASE = "http://127.0.0.1:8793"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


def call(method: str, path: str, body=None, raw=False):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(API_BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = resp.read()
            if raw:
                return resp.status, payload
            return resp.status, json.loads(payload.decode("utf-8"))
    except urllib.error.HTTPError as e:
        payload = e.read()
        if raw:
            return e.code, payload
        return e.code, json.loads(payload.decode("utf-8"))


# ------------------------------------------------------------------ A. 解析器单测
def test_parser() -> None:
    dec = LoraDecoder()
    f = build_frame(dst=0x0003, src=0x0000, seq=7, cmd=0x05)
    frames = dec.feed(f, ts=100.0)
    check("A1 正确帧解析", len(frames) == 1 and frames[0].cmd == 0x05
          and frames[0].dst == 3 and frames[0].src == 0 and frames[0].seq == 7,
          f"n={len(frames)}")
    check("A2 方向判定(主机→从机)", frames[0].direction == "主机→从机",
          frames[0].direction)
    check("A3 命令名映射", cmd_name(0xA0) == "告警上报" and cmd_name(0xB0) == "告警确认"
          and cmd_name(0x85) == "心跳应答")

    up = build_frame(dst=0x0000, src=0x0003, seq=7, cmd=0x85,
                     payload=bytes([0xAA, 0x55]) + bytes(range(18)))  # 载荷内含 AA55
    b = dec.feed(b"\x01\x02\xde\xad" + up, ts=100.0)      # 前置噪声 + 完整帧
    ok = len(b) == 1 and b[0].plen == 20 and b[0].direction == "从机→主机"
    check("A4 噪声重同步+上行帧+载荷含AA55", ok,
          f"n={len(b)} dir={b[0].direction if b else '-'}")
    check("A5 载荷HEX截断展示", "…(20B)" in (b[0].values.get("PAYLOAD") or "")
          and b[0].payload_hex.startswith("AA 55 00 01"),
          str(b[0].values.get("PAYLOAD"))[:60] if b else "-")
    snap = dec.snapshot()
    check("A6 丢弃/重同步统计", snap["bytes_dropped"] == 4 and snap["resync"] >= 1,
          f"dropped={snap['bytes_dropped']} resync={snap['resync']}")

    dec2 = LoraDecoder()
    bad = bytearray(build_frame(dst=1, src=0, seq=1, cmd=0x05))
    bad[-1] ^= 0xFF                                        # 破坏 CRC
    dec2.feed(bytes(bad), ts=100.0)
    s2 = dec2.snapshot()
    check("A7 CRC 错帧计数", s2["crc_err"] == 1 and s2["frames_ok"] == 0,
          f"crc_err={s2['crc_err']} ok={s2['frames_ok']}")

    good = build_frame(dst=1, src=0, seq=2, cmd=0x85)
    for i in range(len(good)):                             # 逐字节分片喂入
        dec2.feed(good[i:i + 1], ts=100.0)
    check("A8 逐字节分片喂入", dec2.snapshot()["frames_ok"] == 1)

    dec3 = LoraDecoder()
    dec3.feed(broadcast := build_frame(dst=0xFFFF, src=0, seq=1, cmd=0x07,
                                       payload=b"hi"), ts=100.0)
    check("A9 广播方向", broadcast and dec3._recent[0].direction == "广播",
          dec3._recent[0].direction)
    unknown = build_frame(dst=1, src=0, seq=1, cmd=0x55, payload=b"x")
    dec3.feed(unknown, ts=100.0)
    check("A10 未知命令码", dec3._recent[-1].values["CMD_NAME"] == "0x55",
          dec3._recent[-1].values["CMD_NAME"])


# ------------------------------------------------------------------ B. API 端到端
def test_api() -> None:
    deadline, status = time.monotonic() + 20, {}
    while time.monotonic() < deadline:
        _, status = call("GET", "/api/status")
        if status.get("state") == "locked":
            break
        time.sleep(0.4)
    check("B1 模拟源已锁定", status.get("state") == "locked" and status.get("mock") is True,
          f"state={status.get('state')} mock={status.get('mock')}")

    _, proto = call("GET", "/api/protocol")
    check("B2 协议链路 kind=lora", proto.get("kind") == "lora"
          and proto.get("layout") == "lora", f"kind={proto.get('kind')}")
    check("B3 已解析出 LoRa 帧", proto.get("frames_ok", 0) > 0,
          f"frames_ok={proto.get('frames_ok')} crc_err={proto.get('crc_err')}")
    expect_cols = ["接收时刻", "方向", "目标地址", "源地址", "Seq", "命令", "载荷长度", "载荷HEX"]
    check("B4 方案口径列名", proto.get("columns") == expect_cols,
          str(proto.get("columns")))

    recents = proto.get("recent") or []
    cmds = {r.get("cmd") for r in recents}
    check("B5 心跳轮询双向帧在表", 0x05 in cmds and 0x85 in cmds, f"cmds={sorted(cmds)}")

    # 等 mock 每 10 轮(2s)一次的 0xA0/0xB0 告警闭环出现在最近帧里
    deadline, seen = time.monotonic() + 12, set()
    while time.monotonic() < deadline and not {0xA0, 0xB0} <= seen:
        _, p = call("GET", "/api/protocol")
        seen = {r.get("cmd") for r in (p.get("recent") or [])}
        time.sleep(1.0)
    check("B6 告警上报/确认帧在表（仅展示）", {0xA0, 0xB0} <= seen, f"seen={sorted(seen)}")

    _, settings = call("GET", "/api/settings")
    layout = settings.get("layout") or {}
    # source：冷启动为 cli（--proto 指定）；运行中激活过方案则为 config，均为合法
    check("B7 帧格式快照", layout.get("kind") == "lora" and layout.get("mode") == "lora"
          and layout.get("source") in ("cli", "config"),
          f"kind={layout.get('kind')} mode={layout.get('mode')} src={layout.get('source')}")

    _, schemes = call("GET", "/api/schemes")
    names = {s.get("name") for s in schemes.get("items", [])}
    check("B8 内置 LoRa 方案存在", "GD300-LoRa主从" in names, str(sorted(names))[:120])

    _, fields = call("GET", "/api/fields")
    ids = [f.get("id") for f in fields.get("fields", [])]
    check("B9 字段表", ids == ["dir", "dst", "src", "seq", "cmd", "plen", "payload"],
          str(ids))

    # 切到文本内置方案再切回 LoRa（验证运行中链路热切换）
    _, act = call("POST", "/api/schemes/" + urllib.parse.quote("GD300-文本行") + "/activate")
    check("B10 切换文本方案", act.get("ok") and act.get("active") == "GD300-文本行",
          str(act.get("active")))
    _, proto_t = call("GET", "/api/protocol")
    check("B11 热切换后链路跟随", proto_t.get("kind") == "text",
          f"kind={proto_t.get('kind')} cols={proto_t.get('columns')}")
    _, act2 = call("POST", "/api/schemes/" + urllib.parse.quote("GD300-LoRa主从") + "/activate")
    _, proto_l = call("GET", "/api/protocol")
    check("B12 切回 LoRa 方案", act2.get("ok") and proto_l.get("kind") == "lora",
          f"kind={proto_l.get('kind')}")

    time.sleep(1.0)
    st, csv = call("GET", "/api/export/samples.csv", raw=True)
    head = csv[:200].decode("utf-8", "replace")
    check("B13 导出采样 CSV", st == 200 and head.startswith("\ufeff") and "," in head,
          head.splitlines()[0][:100] if head else "-")


def main() -> None:
    test_parser()
    print("--- 以下为 API 端到端（需 mock 服务运行中） ---")
    try:
        call("GET", "/api/status")
    except Exception:
        print("[SKIP] API 服务未运行（python service/main.py --mock --port 8793 --proto lora）")
        fails = [r for r in results if not r[1]]
    else:
        test_api()
        fails = [r for r in results if not r[1]]

    print(f"\n共 {len(results)} 项，通过 {len(results) - len(fails)}，失败 {len(fails)}")
    if fails:
        for name, _, detail in fails:
            print(f"  FAIL: {name} {detail}")
        sys.exit(1)


if __name__ == "__main__":
    main()

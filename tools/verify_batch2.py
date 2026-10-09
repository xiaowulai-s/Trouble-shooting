# -*- coding: utf-8 -*-
"""第二批验证脚本：滤波 / 报警全链路 / 导出 / 手动下行（对运行中的 mock 服务）

用法：
    python service/main.py --mock --port 8792   # 另开一个终端
    python tools/verify_batch2.py
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8792"


def call(method, path, body=None, raw=False):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = resp.read()
            if raw:
                return resp.status, payload, {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, json.loads(payload.decode("utf-8"))
    except urllib.error.HTTPError as e:
        payload = e.read()
        if raw:
            return e.code, payload, {k.lower(): v for k, v in e.headers.items()}
        return e.code, json.loads(payload.decode("utf-8"))


def wait_locked(timeout=20.0):
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        _, last = call("GET", "/api/status")
        if last.get("state") == "locked":
            return last
        time.sleep(0.4)
    return last


def main():
    results = []

    # 0) 等待模拟源锁定（下行必须在此之后才允许）
    status = wait_locked()
    results.append(("模拟源已锁定", status.get("state") == "locked",
                    f"state={status.get('state')} mock={status.get('mock')} port={status.get('port')}"))

    # 1) 取当前文本方案 → 另存为带"滤波+上限报警"的自定义方案
    _, body = call("GET", "/api/schemes/" + urllib.parse.quote("GD300-文本行"))
    scheme = body["scheme"]
    scheme["name"] = "第二批-验证"
    scheme["builtin"] = False
    scheme["fields"][0]["filter"] = {"type": "moving_avg", "window": 5, "alpha": 0.2}
    scheme["fields"][0]["alarm"] = {
        "high": {"enabled": True, "limit": 23150, "hysteresis": 5, "debounce_s": 0.3},
        "low": {"enabled": False, "limit": 0, "hysteresis": 0, "debounce_s": 1.0},
    }
    status, saved = call("POST", "/api/schemes", scheme)
    results.append(("保存带滤波+报警的方案", status == 200 and saved["ok"], f"{status}"))
    status, act = call("POST", "/api/schemes/" + urllib.parse.quote("第二批-验证") + "/activate")
    results.append(("激活该方案", status == 200 and act["ok"], f"{status} active={act.get('active')}"))

    # 2) 观察 ~7 s：模拟值在 23132–23168 间摆动，应产生"进入上限/解除"跳变
    time.sleep(7.0)

    status, alarms = call("GET", "/api/alarms?limit=200")
    items = alarms.get("items", [])
    enters = [a for a in items if a.get("active")]
    clears = [a for a in items if not a.get("active")]
    results.append(("报警已产生（上限进入）", status == 200 and len(enters) >= 1,
                    f"{status} enters={len(enters)} clears={len(clears)} "
                    f"first={enters[0]['text'] if enters else '-'}"))
    results.append(("报警能自动解除（滞回+自动解警）", len(clears) >= 1,
                    f"clears={len(clears)} last={clears[0]['text'] if clears else '-'}"))
    results.append(("报警记录带字段名/单位", all(a.get("name") for a in items) if items else False,
                    f"sample={items[0] if items else {}}"))

    # 3) 采样导出 CSV：UTF-8 BOM + 表头含字段名 + 数据行
    status, payload, headers = call("GET", "/api/export/samples.csv", raw=True)
    text = payload.decode("utf-8-sig")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    ok = (status == 200 and "text/csv" in headers.get("content-type", "")
          and len(lines) >= 3 and "频率" in lines[0] and "接收时刻" in lines[0]
          and "attachment" in headers.get("content-disposition", ""))
    results.append(("采样 CSV 导出", ok,
                    f"{status} rows={len(lines) - 1} header={lines[0] if lines else '-'}"))

    # 4) 报警导出 CSV
    status, payload, headers = call("GET", "/api/export/alarms.csv", raw=True)
    text = payload.decode("utf-8-sig")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    ok = (status == 200 and len(lines) >= 2 and "字段名" in lines[0]
          and "进入" in text and "上限" in text)
    results.append(("报警 CSV 导出", ok,
                    f"{status} rows={len(lines) - 1} header={lines[0] if lines else '-'}"))

    # 5) 手动下行（文本，追加换行）
    before = call("GET", "/api/status")[1].get("sent_bytes", 0)
    status, body = call("POST", "/api/send", {"mode": "text", "data": "PING"})
    results.append(("文本下行成功", status == 200 and body.get("ok") and body.get("hex") == "50494E470A",
                    f"{status} {body}"))

    # 6) 手动下行（HEX，不追加换行）
    status, body = call("POST", "/api/send", {"mode": "hex", "data": "AA 55 01", "append_newline": False})
    results.append(("HEX 下行成功", status == 200 and body.get("ok") and body.get("hex") == "AA5501",
                    f"{status} {body}"))

    # 7) 非法 HEX 被拒
    status, body = call("POST", "/api/send", {"mode": "hex", "data": "ZZ"})
    results.append(("非法 HEX 被拒", status == 400, f"{status} {body.get('error')}"))

    # 8) 下行确实写到了链路（sent_bytes 递增）
    after = call("GET", "/api/status")[1].get("sent_bytes", 0)
    results.append(("下行字节计数递增", after > before, f"before={before} after={after}"))

    # 9) 清理：删除自定义方案并切回文本内置
    call("DELETE", "/api/schemes/" + urllib.parse.quote("第二批-验证"))
    call("POST", "/api/schemes/" + urllib.parse.quote("GD300-文本行") + "/activate")
    time.sleep(2.0)                                     # 换方案会重建解析器：等新链路攒几帧
    status, proto = call("GET", "/api/protocol")
    results.append(("切回文本方案", proto.get("kind") == "text" and proto.get("frames_ok", 0) > 0,
                    f"kind={proto.get('kind')} ok={proto.get('frames_ok')}"))

    print("=" * 78)
    for name, ok, detail in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name} -- {detail}")
    failed = [n for n, ok, _ in results if not ok]
    print(f"RESULT: {'ALL PASS' if not failed else 'FAILED -> ' + ', '.join(failed)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
# -*- coding: utf-8 -*-
"""第一批验证脚本：方案 CRUD / 切换 / 字段表生效 / 会话缓冲（对运行中的 mock 服务）"""
import json
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8791"


def call(method, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def main():
    results = []

    # 1) 读取内置 25 字节方案 → 另存为自定义（电池 k=0.1 已在内置里）
    status, body = call("GET", "/api/schemes/GD300-25%20%E5%AD%97%E8%8A%82")
    results.append(("GET 内置25方案", status == 200 and body["ok"],
                    f"{status} fields={len(body.get('scheme', {}).get('fields', []))}"))
    scheme = body["scheme"]

    # 2) 内置方案只读：同名保存必须被拒
    status, body = call("POST", "/api/schemes", scheme)
    results.append(("内置方案只读保护", status == 400, f"{status} {body.get('error')}"))

    # 3) 改名另存 → 成功
    scheme["name"] = "自定义25-验证"
    scheme["builtin"] = False
    scheme["fields"][0]["name"] = "帧类型A"        # 改个显示名，验证字段表生效
    status, body = call("POST", "/api/schemes", scheme)
    results.append(("另存自定义方案", status == 200 and body["ok"], f"{status}"))

    # 4) 切到该方案 → 解析器换二进制、列名随之变化
    status, body = call("POST", "/api/schemes/%E8%87%AA%E5%AE%9A%E4%B9%8925-%E9%AA%8C%E8%AF%81/activate")
    results.append(("激活自定义25方案", status == 200 and body["ok"],
                    f"{status} active={body.get('active')}"))
    time.sleep(2.0)
    status, proto = call("GET", "/api/protocol")
    ok = (proto.get("kind") == "binary" and proto.get("frames_ok", 0) > 0
          and proto.get("crc_err") == 0 and proto["columns"][1] == "帧类型A")
    results.append(("二进制链路解析+字段表生效", ok,
                    f"kind={proto.get('kind')} ok={proto.get('frames_ok')} "
                    f"err={proto.get('crc_err')} col1={proto['columns'][1]}"))

    # 5) 校验非法：空字段表
    bad = dict(scheme)
    bad["name"] = "非法空字段"
    bad["fields"] = []
    status, body = call("POST", "/api/schemes", bad)
    results.append(("空字段表被拒", status == 400, f"{status} {body.get('error')}"))

    # 6) 校验非法：CRC 偏移越界
    bad2 = json.loads(json.dumps(scheme))
    bad2["name"] = "非法CRC"
    bad2["frame"]["custom"]["crc_offset"] = 99
    status, body = call("POST", "/api/schemes", bad2)
    results.append(("CRC 越界被拒", status == 400, f"{status} {body.get('error')}"))

    # 7) 会话缓冲已写入二进制采样
    status, store = call("GET", "/api/store?limit=5")
    results.append(("会话缓冲落盘", status == 200 and store["stats"]["rows"] > 0,
                    f"rows={store['stats']['rows']} buffered={store['stats']['buffered']}"))

    # 8) 切回文本内置方案
    status, body = call("POST", "/api/schemes/GD300-%E6%96%87%E6%9C%AC%E8%A1%8C/activate")
    time.sleep(1.5)
    status, proto = call("GET", "/api/protocol")
    results.append(("切回文本方案", proto.get("kind") == "text" and proto.get("frames_ok", 0) > 0,
                    f"kind={proto.get('kind')} ok={proto.get('frames_ok')} col={proto['columns'][1]}"))

    # 9) 删除自定义方案 + 不存在方案 404
    status, body = call("DELETE", "/api/schemes/%E8%87%AA%E5%AE%9A%E4%B9%8925-%E9%AA%8C%E8%AF%81")
    results.append(("删除自定义方案", status == 200 and body["ok"], f"{status}"))
    status, body = call("GET", "/api/schemes/%E4%B8%8D%E5%AD%98%E5%9C%A8")
    results.append(("不存在方案 404", status == 404, f"{status}"))

    print("=" * 72)
    for name, ok, detail in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name} -- {detail}")
    failed = [n for n, ok, _ in results if not ok]
    print(f"RESULT: {'ALL PASS' if not failed else 'FAILED -> ' + ', '.join(failed)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
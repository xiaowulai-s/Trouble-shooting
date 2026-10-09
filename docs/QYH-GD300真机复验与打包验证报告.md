# QYH-GD300 上位机 · 真机复验 + exe 重新打包 验证报告

- 日期：2026-10-09
- 范围：第二批（曲线 / 滤波 / 报警 / 导出 / 下行）交付后的收尾——**真实串口复验** 与 **单文件 exe 重新打包**
- 承接：`QYH-GD300第一批验证报告.md`、`QYH-GD300第二批验证报告.md`
- 环境：Windows 11（10.0.26300），Python 3.14.2，PyInstaller 6.19.0

---

## 一、真机复验结果

### 1.1 串口环境

| 端口 | 设备 | 说明 |
| --- | --- | --- |
| COM3 | Silicon Labs CP210x USB to UART Bridge（`VID_10C4&PID_EA60`） | USB 转串口，**目标接收端适配器** |
| COM1 | 通信端口（`ACPI\PNP0501`） | 主板 legacy 口，无数据 |

> 说明：上一轮报告记录的接收端为 COM4，本次枚举已无 COM4；目标适配器现挂载在 **COM3**。

### 1.2 真实串口模式启动

```powershell
python service/main.py        # 无 --mock：真实串口
```

启动后 `/api/status` 立即体现真实模式：

```json
{"state":"searching","port":null,"scan":{"index":2,"total":2,"port":"COM1"},
 "total_bytes":0,"rate":0.0,"mock":false,"error":null,"sent_bytes":0}
```

| 检查项 | 结果 | 证据 |
| --- | --- | --- |
| 真实串口模式（非 mock） | ✅ | `mock:false` |
| 尝试打开候选口未报错 | ✅ | `error:null`（COM3/COM1 均可打开读） |
| 扫描顺序 USB 优先 | ✅ | 先 COM3（USB/有 VID）后 COM1（legacy），与 `serial_io._list_ports` 一致 |
| 仅绑 127.0.0.1 | ✅ | 见 2.2 自检 PASS |
| **扫描期只读（不写串口）** | ✅ | 见 1.3 |

### 1.3 扫描期只读铁律（真机态验证）

接收端未锁定期间（`state=searching`）调用手动下行接口：

```powershell
curl.exe -X POST http://127.0.0.1:8766/api/send -H "Content-Type: application/json" \
  -d '{"mode":"text","data":"PING"}'
```

```
{"ok":false,"error":"接收端尚未锁定，禁止下行（扫描期只读）"}   HTTP=409
```

调用后 `/api/status` 的 `sent_bytes` 仍为 `0`：

```json
{"state":"searching","scan":{"index":2,"total":2,"port":"COM1"},"sent_bytes":0}
```

结论：扫描/识别期 `serial_io.SerialHub.send()` 以 `state != "locked"` 拒绝写入（HTTP 409），
**未向任何串口写入一个字节**，符合硬约束。

### 1.4 接收端数据（阻塞项）

对 COM3 用固定 `115200/8/N/1` 直读（只读，不写）：

| 采样方式 | 时长 | 收到字节 |
| --- | --- | --- |
| 直读 COM3 | 4 s | **0** |
| 直读 COM3 | 8 s | **0** |
| 服务探测 COM3 | 6 × 1 s 窗口 | **0** |

COM3 可正常打开（无 `error`），但**当前设备未输出任何数据**，服务因此一直停留在
`searching`，无法锁定，也就无法进入数据级复验。

> 「真机数据级」复验项 —— 帧解析 / 表格 / 曲线 / 滤波 / 报警进入与解除 / 导出 / 真实下行 ——
> **本轮未完成**，阻塞于设备无输出（非上位机缺陷）。

### 1.5 待设备就绪后的复验步骤（沿用约定）

1. 确认目标设备上电且持续输出；同一串口**只能被一个程序占用**，先关闭其它串口工具。
2. `python service/main.py`（真实串口，识别前只读）→ 打开 `http://127.0.0.1:8766`。
3. 确认顶栏锁定 **COM3**、原始数据流与表格出现数据、曲线联动绘制。
4. 「设置 → 方案」为某字段配置 `moving_avg` 滤波 + 上限报警，观察曲线平滑度与报警进入/解除。
5. 锁定后用手动下行发一条文本命令，确认 `sent_bytes` 递增、设备有响应；导出采样/报警 CSV 与曲线 PNG。
6. 退出程序，确认 `%TEMP%\QYH-GD300` 无 `session.db` 残留。

---

## 二、exe 重新打包结果

### 2.1 打包

```powershell
powershell -ExecutionPolicy Bypass -File build/build_exe.ps1
```

- PyInstaller 6.19.0 / Python 3.14.2 / Windows-64bit-intel
- `console=False` 单文件桌面壳，静态资源 `web/static` 已一并打包
- 产物：

```
D:\Demo\Trouble shooting\build\dist\QYH-GD300上位机.exe
大小 28,584,088 字节（27.3 MB）  构建时间 2026-10-09 12:33
```

### 2.2 双链路自检（对产物 exe 执行）

二进制链路（`--proto 25`）：

```
[PASS] GET /api/status -- state=searching mock=True
[PASS] GET / (static UI) -- HTTP 200, 65141 bytes
[PASS] GET /api/settings -- app_name=QYH-GD300 故障定位系统 max_len=32
[PASS] GET /api/protocol -- kind=binary layout=25 frames_ok=5 err=0 resync=0 cols=11
[PASS] bind 127.0.0.1 only -- not reachable from LAN, as expected
[GD300] self-check: ALL PASS
```

文本行链路（`--proto text`）：

```
[PASS] GET /api/status -- state=searching mock=True
[PASS] GET / (static UI) -- HTTP 200, 65141 bytes
[PASS] GET /api/settings -- app_name=QYH-GD300 故障定位系统 max_len=32
[PASS] GET /api/protocol -- kind=text layout=text frames_ok=5 err=0 resync=0 cols=2
[PASS] bind 127.0.0.1 only -- not reachable from LAN, as expected
[GD300] self-check: ALL PASS
```

打包脚本退出码 `0`；两条链路自检均 **ALL PASS**，产物可直接双击启动桌面端。

---

## 三、结论与未完成项

| 项 | 状态 |
| --- | --- |
| 真实串口模式启动、USB 优先扫描、绑定 127.0.0.1 | ✅ |
| 扫描期只读（`/api/send` 409、`sent_bytes` 恒 0） | ✅ |
| exe 重新打包 + 双链路自检 ALL PASS | ✅ |
| 真机**数据级**复验（帧/表格/曲线/滤波/报警/导出/下行） | ⬜ 阻塞：COM3 设备当前 0 字节输出 |

未完成项仅一项，且为外部条件（设备未输出）所致；上位机侧无缺陷。设备就绪后按 1.5 步骤即可闭环。

---

## 四、复现命令

```powershell
# 真机复验
python service/main.py                                  # 真实串口
curl.exe -s http://127.0.0.1:8766/api/status            # 看 state / mock / sent_bytes
curl.exe -X POST http://127.0.0.1:8766/api/send `
    -H "Content-Type: application/json" -d '{\"mode\":\"text\",\"data\":\"PING\"}'   # 扫描期应 409

# 打包 + 双链路自检
powershell -ExecutionPolicy Bypass -File build/build_exe.ps1
```
# QYH-GD300 上位机 · 第三批（LoRa 主从通用帧只读监听）验证报告

- 日期：2026-10-12
- 范围：LoRa 主从通用帧解析接入（**只读监听**，不做主机模拟收发）；0xA0/0xB0 等上报帧**仅表格展示**，**不做报警联动**（用户明确）
- 模式：**mock（内置模拟数据源，`--proto lora`）**；真实链路数据级复验仍属遗留（见五）
- 承接：第二批报告 `QYH-GD300第二批验证报告.md`；真机复验报告 `QYH-GD300真机复验与打包验证报告.md`
- 协议依据：`docs/LoRa主机从机通信协议说明书.md` v1.0（其中【待定义】【新增定义·待确认】条目见"五、边界"）

---

## 一、交付物清单

| 类型 | 文件 | 说明 |
| --- | --- | --- |
| 新建 | `service/protocol_lora.py` | LoRa 通用帧解析层（纯计算无 IO）：变长切帧 + CRC-16/MODBUS 校验 + 方向/命令名判定 + `build_frame` 组帧（供 mock 与自测）；**节点聚合**（按地址统计 frames/req/resp/report/last_ts，随 snapshot 输出） |
| 新建 | `tools/verify_lora.py` | 第三批验证脚本（A 组解析器单测 + B 组 API 端到端，共 32 项，端口 8793） |
| 新建 | `service/archive.py` | 长期归档（**可选，默认关闭**）：按天 JSONL 存帧原始记录（`{"ts","kind","hex"}`，文本链路为 `{"ts","kind":"text","text"}`），总大小超 `max_mb` 从最旧轮转删除，写失败静默降级不影响监听；配置合并写入 settings.json |
| 修改 | `service/layout_config.py` | 新增链路 key `lora`：`MODE_LORA`、`PROTO_CHOICES`、各解析/方案分支返回 `LORA_LAYOUT`、`describe()` kind 增加 `"lora"` |
| 修改 | `service/schema.py` | 内置方案 `GD300-LoRa主从`（7 个 key 来源字段，全部 `curve:false`）；方案校验允许 `kind:"lora"`；`builtin_for_layout("lora")` 可解析 |
| 修改 | `service/main.py` | `_make_decoder` 增加 lora 分支；`_kind_for_layout` 辅助函数；解码器**换实例条件**推广（key 不同或任一方为 text/lora 时换实例，避免 lora 解析器被误调 `set_layout`）；`set_mock_text` → `set_mock_proto`；`/api/send` 增加 **LoRa 链路禁写拦截（409）**；**长期归档接线**（`ArchiveStore` 实例 + 通信线程批量落盘 + `/api/archive` 查询/开关/下载三端点 + 退出关闭） |
| 修改 | `service/serial_io.py` | `MockPort` 三形态 `proto ∈ {"frame","text","lora"}`；`_make_lora()` 模拟主从流量（从机 1~8 心跳 0x05↔0x85，每 10 轮从机 3 发 0xA0 + 主机 0xB0） |
| 修改 | `service/analysis.py` | 字符串值直通：`source.type="key"` 取到字符串（方向/命令名/载荷 HEX）时直接进表格，不参与换算/滤波/报警，曲线侧 value 保持 None。**文本链路的非数字值同样受益** |
| 修改 | `service/desktop_main.py` | mock 形态跟随 `hub.set_mock_proto(layout.key)` |
| 修改 | `web/static/index.html` | 方案摘要/下拉标注/帧格式文案支持 lora；自定义帧编辑框对 `kind∈{text,lora}` 隐藏；发送面板 **LoRa 只读态**（整组控件停用 + 「LoRa 只读」标签，随链路热切换跟随）；**从机状态面板**（仅 lora 显示：在线/超时/响应率/上报数，10s 无帧判离线）；0xA0/0xA1 表格行着色（仅展示）；设置对话框新增**长期归档开关与归档文件列表（下载链接）** |

### 数据流水线（第三批扩展部分）

```
串口字节 → protocol_lora.LoraDecoder（变长切帧 + CRC）
        → analysis.Analyzer（字段表取值；字符串直通）
            ├─→ 推页面：表格行（方向/目标地址/源地址/Seq/命令/载荷长度/载荷HEX）
            └─→ 会话缓冲 + WS 广播（与既有链路一致）
```

### 关键设计说明

- **帧结构与偏移**（依据说明书第 3 章，CRC 范围为【新增定义·待确认】）：

  ```
  AA 55 | Dst(2B LE) | Src(2B LE) | Seq(1B) | Cmd(1B) | Len(1B) | Payload(N) | CRC16(2B LE)
  └ 0 ─┘└ 2 ────────┘└ 4 ────────┘└ 6 ─────┘└ 7 ─────┘└ 8 ─────┘            └ 9+Len ──────┘
  固定开销 11B（含 CRC 2B）；载荷起点 = 9（帧头2+目标2+源2+Seq1+Cmd1+Len1）
  ```

- **变长切帧策略**：找 `AA 55` → 收满 9B 帧头 → 按 `Len` 计算整帧长（11+Len）→ CRC 校验；失败则滑 1 字节重同步并计数（`resync`/`bytes_dropped`）。
- **方向判定**：`dst==0xFFFF` 广播；`src==0` 主机→从机；`dst==0` 从机→主机（说明书第 2 章地址约定）。
- **载荷展示**：HEX 超 12 字节截断为 `AA 55 00 01 … (NB)` 形式；未知命令码显示 `0xNN`。
- **范围约定（用户确认）**：0xA0 告警上报 / 0xB0 告警确认等上报帧**仅作为表格行展示**，不接入报警面板、不产生报警事件。mock 中每 10 轮生成一次 0xA0+0xB0 闭环流量用于展示验证。
- **表格列由方案字段表驱动**：内置方案 7 字段（dir/dst/src/seq/cmd/plen/payload）→ 列名 `接收时刻,方向,目标地址,源地址,Seq,命令,载荷长度,载荷HEX`。

---

## 二、验收标准对照

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| 变长帧解析（含载荷内出现 AA 55） | ✅ | A1/A4/A5 PASS |
| 噪声重同步 + 丢弃/重同步统计 | ✅ | A4/A6 PASS（dropped=4 resync=1） |
| CRC 错帧不解析并计数 | ✅ | A7 PASS（crc_err=1 frames_ok=0） |
| 分片喂入（逐字节） | ✅ | A8 PASS |
| 方向判定（主机→从机/从机→主机/广播） | ✅ | A2/A4/A9 PASS |
| 未知命令码容错 | ✅ | A10 PASS |
| 心跳轮询双向帧上表（0x05/0x85） | ✅ | B5 PASS |
| 0xA0/0xB0 仅表格展示（不联动报警） | ✅ | B6 PASS（seen 含 160/176，无报警推送） |
| 链路热切换（text ↔ lora） | ✅ | B10~B12 PASS，切换后 kind/列名跟随 |
| 导出采样 CSV（LoRa 列头） | ✅ | B13 PASS，表头 `接收时刻,方向,目标地址,源地址,Seq,命令,载荷长度,载荷HEX` |
| LoRa 链路禁止手动下行（风险防控） | ✅ | B14/B15/B16 PASS：lora→409 拦截，text→200 正常，切回 lora→409 仍拦截 |
| 从机状态聚合（在线/问答配对/上报计数） | ✅ | B17/B18 PASS：8 从机 req/resp 计数齐全、从机 3 的 0xA0 上报计数=1 |
| 长期归档（帧原始记录，默认关闭） | ✅ | B19~B22 PASS：默认 off、开启后产出当日 `监听_YYYYMMDD.jsonl`、行格式 `ts+kind+(hex\|text)` 合法、关闭后停止写入 |

---

## 三、mock 模式验证结果

服务：`python service/main.py --mock --port 8793 --proto lora`（仅绑 127.0.0.1）；
脚本：`python tools/verify_lora.py`。最终运行输出（**共 32 项，通过 32，失败 0**）：

```
[PASS] A1 正确帧解析 -- n=1
[PASS] A2 方向判定(主机→从机) -- 主机→从机
[PASS] A3 命令名映射
[PASS] A4 噪声重同步+上行帧+载荷含AA55 -- n=1 dir=从机→主机
[PASS] A5 载荷HEX截断展示 -- AA 55 00 01 02 03 04 05 06 07 08 09 …(20B)
[PASS] A6 丢弃/重同步统计 -- dropped=4 resync=1
[PASS] A7 CRC 错帧计数 -- crc_err=1 ok=0
[PASS] A8 逐字节分片喂入
[PASS] A9 广播方向 -- 广播
[PASS] A10 未知命令码 -- 0x55
--- 以下为 API 端到端（需 mock 服务运行中） ---
[PASS] B1 模拟源已锁定 -- state=locked mock=True
[PASS] B2 协议链路 kind=lora -- kind=lora
[PASS] B3 已解析出 LoRa 帧 -- frames_ok=34 crc_err=0
[PASS] B4 方案口径列名 -- ['接收时刻', '方向', '目标地址', '源地址', 'Seq', '命令', '载荷长度', '载荷HEX']
[PASS] B5 心跳轮询双向帧在表 -- cmds=[5, 133, 160, 176]
[PASS] B6 告警上报/确认帧在表（仅展示） -- seen=[5, 133, 160, 176]
[PASS] B7 帧格式快照 -- kind=lora mode=lora src=cli
[PASS] B8 内置 LoRa 方案存在 -- ['GD300-14 字节', 'GD300-25 字节', 'GD300-LoRa主从', 'GD300-文本行']
[PASS] B9 字段表 -- ['dir', 'dst', 'src', 'seq', 'cmd', 'plen', 'payload']
[PASS] B10 切换文本方案 -- GD300-文本行
[PASS] B11 热切换后链路跟随 -- kind=text cols=['接收时刻', '频率(Hz)']
[PASS] B12 切回 LoRa 方案 -- kind=lora
[PASS] B13 导出采样 CSV -- 接收时刻,方向,目标地址,源地址,Seq,命令,载荷长度,载荷HEX
[PASS] B14 LoRa 链路禁止手动下行 -- HTTP 409 err=LoRa 链路为只读监听：禁止手动下行（写入会向真实主从网络注入帧）
[PASS] B15 文本链路可下行（拦截仅限 LoRa） -- HTTP 200
[PASS] B16 切回 LoRa 后仍拦截 -- HTTP 409
[PASS] B17 问答配对计数（主机点名→从机应答） -- {'0x1': (1, 1), '0x2': (1, 1), '0x3': (2, 1), '0x4': (2, 2), '0x5': (2, 2), '0x6': (1, 1), …}
[PASS] B18 告警/事件上报计数 -- {'0x3': 1}
[PASS] B19 归档默认关闭 -- enabled=False max_mb=200
[PASS] B20 开启归档产出当日文件 -- ok=True files=['监听_20261012.jsonl']
[PASS] B21 帧原始记录格式合法 -- HTTP 200 rows=28 sample={'ts': 1791791968.6667278, 'kind': 'lora', 'hex': 'AA 55 06 00 00 00 2 …}
[PASS] B22 关闭归档后停止写入 -- size 2293 -> 2293
```

补充观察：B3 运行期间 `frames_ok` 持续增长、`crc_err=0`（mock 流量本身 CRC 全对）；B6 轮询窗口内最近帧稳定出现 0xA0（告警上报）与 0xB0（告警确认）行。验证结束后已通过 API 把激活方案恢复为 `GD300-文本行`，并停止 mock 服务。

**补做 #6 风险防控（2026-10-12）**：LoRa 只读监听定位下，写串口 = 向真实主从网络注入帧（冒充主机/从机）。故 `/api/send` 在 LoRa 链路一律返回 409（与扫描期同口径），文本/二进制链路不受影响；前端发送面板随链路热切换显示「LoRa 只读」并停用整组控件（`sendUiSig` 缓存签名，避免逐帧刷 DOM）。

**补做 #4/#5/#7 从机视图（2026-10-12）**：解析层按地址聚合节点状态（`_track`：主机点对点请求记到**被点名从机**头上，广播不计入分母；0x81~0x87 计应答、0xA0/0xA1 计上报），随 `snapshot()` 的 `nodes` 键输出原始计数与最后帧时刻；前端「从机状态」面板仅 lora 链路显示——在线/超时（10s 无帧）、点对点响应率（应答/请求）、上报数；0xA0/0xA1 表格行加浅红底着色（仍仅展示，不联动报警）。在线阈值 10s 为前端常数（`ONLINE_S`），真实轮询周期确认后可调。

**补做 #8 长期归档（2026-10-12，用户确认"帧原始记录"口径）**：会话缓冲"退出即清"是硬约束，现场长跑需要一份**可选**的持久落盘。归档只存帧原始记录（`ts` + `kind` + 整帧 HEX / 文本整行），不存分析结果——协议口径日后修正时历史数据仍可重新解析。实现：新建 `service/archive.py`（`ArchiveStore` 按天写 `archives/监听_YYYYMMDD.jsonl`，双检 enabled、跨天重开、每批 flush、总大小超 `max_mb` 从最旧轮转删除、写失败静默降级）；`main.py` 在通信线程回调里批量落盘并暴露 `GET/POST /api/archive`、`GET /api/archive/download?name=`（文件名白名单防目录穿越）；前端设置对话框加复选框开关与文件下载列表（随保存提交、即时生效）。默认关闭（settings.json `"archive": {"enabled": false, "max_mb": 200}`，合并写入）；`archives/` 为运行时产物，不纳入提交。验证 B19~B22（默认关闭 / 开启产出当日文件 / 行格式合法 / 关闭后停止写入）。

---

## 四、本轮修复记录

1. **载荷偏移 bug（解析器真 bug，A5 自测抓到）**：`_build` 最初写成 `raw[11:11+plen]`（把固定开销 11 当成载荷起点），导致载荷 HEX 以 `00 01 …` 开头、丢失帧内真实载荷头。正确起点为 **9**（帧头2+目标2+源2+Seq1+Cmd1+Len1；固定开销 11 含 CRC 2B）。修复后 A5 断言加强为 `payload_hex.startswith("AA 55 00 01")`。
2. **B7 端点修正**：帧格式快照不在 `GET /api/layout`（不存在），实际在 `GET /api/settings` 的 `"layout"` 键下。
3. **B7 source 断言放宽**：激活方案会把 `source` 置为 `config` 并持久化，冷启动时才是 `cli`；断言放宽为 `source ∈ ("cli","config")`，两者均合法。
4. **前端插入回归（编辑过程抓到）**：`updateProtoStats` 末尾的 `setColumns(s.columns);` 一度被新块挤出函数外，`node --check` 语法检查前发现并归位；B17/B18 首跑还暴露时序问题——从机节点需等主机轮询到（每从机约 1.6s）、0xA0 每 10 轮（约 16s）才出现，改为轮询等待（截止 25s）。

---

## 五、已知边界与待确认项

- **报警联动明确不做**（用户确认）：0xA0/0xA1/0xB0/0xB1 仅作为表格行展示；后续如需联动，需重新定义告警码→字段的映射语义。
- **协议说明书【待定义】项未落地**（见其第 10 章）：0x01~0x07/0x81~0x87 各命令载荷结构、0xA0 告警码表、0xA1 事件码表均未定义——当前一律按"不透明字节"以载荷 HEX 展示，不解释内部字段。
- **【新增定义·待确认】**：帧头线序（AA 在前）、CRC 计算范围（自目标地址起至载荷末，不含帧头）、字节序（多字节字段小端、CRC 小端）。真机接入前需与需求方确认；若与实机不符，仅需调整 `protocol_lora.py` 顶部常量（`CRC_START_OFFSET`/`CRC_ORDER`/`BYTE_ORDER`）。
- **真机数据级复验遗留**：沿用真机复验报告结论——COM3 适配器 0 字节输出未解除，帧解析/表格/曲线的真机级验证待设备供电后补做。
- **exe 重打包** → **已完成**（2026-10-12 重新打包并三链路自检通过，见六）。
- **Seq 去重**：说明书 Seq 去重窗口【待定义】，本批未实现去重（监听场景影响有限）。

---

## 六、exe 重新打包结果（2026-10-12 补做）

命令：`powershell -ExecutionPolicy Bypass -File build/build_exe.ps1`（PyInstaller 6.19.0 / Python 3.14.2 / Windows-64bit-intel，`console=False` 单文件桌面壳，静态资源一并打包）。

- 产物：`build/dist/QYH-GD300上位机.exe`，**27.3 MB**，构建退出码 `0`

产物自检（`--mock --self-check --proto …`）**三链路全部 ALL PASS**：

| 链路 | kind | frames_ok | crc_err | 列数 | 结果 |
| --- | --- | --- | --- | --- | --- |
| `--proto 25`（二进制定长，脚本内置） | binary | 5 | 0 | 11 | ✅ |
| `--proto text`（文本行，脚本内置） | text | 5 | 0 | 2 | ✅ |
| `--proto lora`（LoRa 主从，本轮补跑） | lora | 4 | 0 | 8 | ✅ |

- 绑定 127.0.0.1 / 局域网不可达 ✅；静态 UI 65,342 字节正常 ✅；LoRa 列 8 列（`接收时刻,方向,目标地址,源地址,Seq,命令,载荷长度,载荷HEX`）与方案口径一致 ✅。

---

## 七、后续建议

1. 与需求方确认第 3 章【新增定义·待确认】三项 + 第 10 章载荷结构，再迭代 `protocol_lora.py` 的载荷解释。
2. 设备上电后做真机数据级复验（LoRa 串口透传链路 → 监听解析），补录本报告"真机"小节；若复验引发代码改动，再重新打包并重跑三链路自检。
3. 真机长跑前按磁盘余量与总线流量估算调整归档上限（settings.json `"archive"."max_mb"`，默认 200 MB）；`archives/` 目录为运行时产物，不纳入提交。

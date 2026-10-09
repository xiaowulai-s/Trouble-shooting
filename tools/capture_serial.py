# -*- coding: utf-8 -*-
"""GD300 接收端串口原始数据抓取（一次性诊断脚本，用于反推真实帧格式）

用法: python tools/capture_serial.py
"""
import serial
import serial.tools.list_ports as lp
import time
import sys

BAUD_LIST = [115200, 9600, 57600, 38400, 19200, 4800]
DURATION = 6.0

ports = list(lp.comports())
print("可用串口:")
for p in ports:
    print(f"  {p.device} | {p.description} | {p.hwid}")

# 优先抓有 VID 的 USB 转串口，排除主板自带的 legacy 口
usb_ports = [p.device for p in ports if p.vid is not None]
targets = usb_ports or [p.device for p in ports]
print("抓取目标:", targets)

for dev in targets:
    for baud in BAUD_LIST:
        try:
            ser = serial.Serial(dev, baud, bytesize=8, parity="N",
                                stopbits=1, timeout=0.2)
        except Exception as e:
            print(f"[{dev}] 打开失败: {e}")
            break

        buf = bytearray()
        t0 = time.time()
        while time.time() - t0 < DURATION:
            try:
                chunk = ser.read(4096)
            except Exception as e:
                print(f"[{dev}] 读取异常: {e}")
                break
            if chunk:
                buf.extend(chunk)
        try:
            ser.close()
        except Exception:
            pass

        n = len(buf)
        print("=" * 72)
        print(f"[{dev} @ {baud}] 收到 {n} 字节, 用时 {time.time() - t0:.1f}s")
        if n == 0:
            continue

        for off in range(0, min(n, 512), 16):
            row = buf[off:off + 16]
            hexs = " ".join(f"{b:02X}" for b in row)
            asc = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
            print(f"{off:06X}  {hexs:<48}  |{asc}|")

        freq = {}
        for b in buf:
            freq[b] = freq.get(b, 0) + 1
        top = sorted(freq.items(), key=lambda x: -x[1])[:12]
        print("高频字节:", " ".join(f"{b:02X}x{c}" for b, c in top))

        for sync in (b"\xaa\x55", b"\x55\xaa", b"\x7e"):
            idx = buf.find(sync)
            if idx >= 0:
                print(f"发现候选同步头 {sync.hex().upper()} 于偏移 {idx}")

        print(f"[{dev}] 有数据，停止换波特率尝试")
        sys.exit(0)

print("所有目标串口在全部波特率下均无数据。")
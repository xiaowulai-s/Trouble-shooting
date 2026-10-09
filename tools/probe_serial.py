# -*- coding: utf-8 -*-
"""GD300 接收端串口控制线探测 + 下行命令触发测试

用于判断接收端是否"只在上位机先开口后才转发数据"。
用法: python tools/probe_serial.py [COM3] [115200]
"""
import serial
import sys
import time

dev = sys.argv[1] if len(sys.argv) > 1 else "COM3"
baud = int(sys.argv[2]) if len(sys.argv) > 2 else 115200

s = serial.Serial(dev, baud, bytesize=8, parity="N", stopbits=1, timeout=0.5)
print(f"已打开 {dev} @ {baud}")
print(f"控制线: CTS={s.cts} DSR={s.dsr} CD={s.cd} RI={s.ri}")
print(f"默认 DTR={s.dtr} RTS={s.rts}")

s.dtr = True
s.rts = True
time.sleep(1.0)
print(f"拉高 DTR/RTS 后: CTS={s.cts} DSR={s.dsr} CD={s.cd}")
d = s.read(8192)
print(f"  读到 {len(d)} 字节: {d[:200].hex(' ').upper()}")

s.dtr = False
s.rts = False
time.sleep(1.0)
d = s.read(8192)
print(f"拉低 DTR/RTS 后读到 {len(d)} 字节: {d[:200].hex(' ').upper()}")

s.reset_input_buffer()

# 试探 1：文档 3.5 节的广播"读配置"下行帧
cmd = bytes([0x55, 0xAA, 0x01, 0x10, 0xFF, 0xFF, 0x00, 0x01, 0x00])
s.write(cmd)
s.flush()
time.sleep(1.5)
d = s.read(8192)
print(f"发 0x10 广播读配置({cmd.hex(' ').upper()}) 后读到 {len(d)} 字节: {d[:200].hex(' ').upper()}")

s.reset_input_buffer()

# 试探 2：常见"唤醒"字符 / 换行
for probe in (b"\r\n", b"\x00", b"AT\r\n"):
    s.write(probe)
    s.flush()
    time.sleep(0.8)
    d = s.read(8192)
    print(f"发 {probe!r} 后读到 {len(d)} 字节: {d[:120].hex(' ').upper()}")

s.close()
print("探测结束。")
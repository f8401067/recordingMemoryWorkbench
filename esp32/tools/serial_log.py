#!/usr/bin/env python3
"""抓一段串口日志（macOS 上没有 timeout 命令，用这个代替）

    python3 tools/serial_log.py [-p PORT] [-s SECONDS] [-b BAUD]

按 Ctrl-C 也能提前结束。
"""
import argparse
import sys
import time

import serial


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-p", "--port", default="/dev/cu.usbmodem13101")
    ap.add_argument("-s", "--seconds", type=float, default=15.0)
    ap.add_argument("-b", "--baud", type=int, default=115200)
    ap.add_argument("-r", "--reset", action="store_true", help="先复位一次再抓")
    args = ap.parse_args()

    try:
        ser = serial.Serial(args.port, args.baud, timeout=0.2)
    except Exception as e:  # noqa: BLE001
        print(f"打开 {args.port} 失败: {e}", file=sys.stderr)
        return 1

    if args.reset:
        # EN 拉低再放开 = 按一下复位键
        ser.dtr = False
        ser.rts = True
        time.sleep(0.15)
        ser.rts = False
        time.sleep(0.05)

    end = time.time() + args.seconds
    try:
        while time.time() < end:
            data = ser.read(4096)
            if data:
                sys.stdout.write(data.decode("utf-8", "replace"))
                sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

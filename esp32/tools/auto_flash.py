#!/usr/bin/env python3
"""守株待兔式烧录：盯着 /dev/cu.usb* 出现，一出现立刻烧 merged 镜像。

用法：
    python tools/auto_flash.py [--port /dev/cu.usbmodem13101] [--timeout 300] [--baud 921600]

背景：ESP32-S3 用原生 USB-Serial-JTAG，复位时串口节点会短暂消失；
      USB 线接触不良时更明显。所以这里轮询到端口就立刻动手，不等人。
"""

import argparse
import glob
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 合并镜像的名字在 ESP-IDF 6 里是 merged-binary.bin，早期是 <项目>_merged.bin。
# 两个都认，取最新的那个（以前写死成 recall_mic_merged.bin，刷进去的一直是旧镜像）。
def _find_image():
    build = os.path.join(ROOT, "build")
    cands = [
        os.path.join(build, "merged-binary.bin"),
        os.path.join(build, "recall_mic_merged.bin"),
    ]
    existing = [p for p in cands if os.path.isfile(p)]
    if not existing:
        return cands[0]
    return max(existing, key=os.path.getmtime)


IMAGE = _find_image()

CANDIDATES = (
    "/dev/cu.usbmodem*",
    "/dev/cu.usbserial*",
    "/dev/cu.wchusbserial*",
    "/dev/tty.usbmodem*",
    "/dev/tty.usbserial*",
)


def find_ports():
    ports = []
    for pat in CANDIDATES:
        ports.extend(glob.glob(pat))
    # cu.* 优先（callout，不会被 modem 控制信号量卡住）
    ports.sort(key=lambda p: (not os.path.basename(p).startswith("cu."), p))
    return ports


def wait_port(prefer, timeout, log):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        ports = find_ports()
        if prefer and prefer in ports:
            return prefer
        if ports:
            # 端口刚出现时可能还没稳定，等 0.4s 再确认一次
            p = ports[0]
            time.sleep(0.4)
            if os.path.exists(p):
                return p
            continue
        if last != "absent":
            log("等待 USB 串口出现 ...（插上线 / 按一下复位键）")
            last = "absent"
        time.sleep(0.3)
    return None


def run(cmd, log):
    log("$ " + " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.stdout:
        sys.stdout.write(r.stdout)
    if r.stderr:
        sys.stderr.write(r.stderr)
    return r.returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None)
    ap.add_argument("--timeout", type=int, default=300, help="等端口出现的最长秒数")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--tries", type=int, default=6, help="烧录重试次数")
    ap.add_argument("--no-monitor", action="store_true")
    args = ap.parse_args()

    def log(msg):
        print(msg, flush=True)

    if not os.path.exists(IMAGE):
        log("找不到镜像 %s，先跑 idf.py build + merge-bin" % IMAGE)
        return 2

    port = args.port
    if port and not os.path.exists(port):
        log("指定端口 %s 不存在，转入自动等待" % port)
        port = None
    if port is None:
        port = wait_port(None, args.timeout, log)
    if port is None:
        log("超时：%ds 内没等到 USB 串口" % args.timeout)
        return 1

    log("捕获端口 %s，开始烧录 %s (%.0f KB)" % (port, IMAGE, os.path.getsize(IMAGE) / 1024.0))

    base = [
        sys.executable, "-m", "esptool",
        "--chip", "esp32s3",
        "--port", port,
    ]
    bauds = [args.baud, 460800, 230400, 115200]
    for attempt in range(1, args.tries + 1):
        baud = bauds[min(attempt - 1, len(bauds) - 1)]
        cmd = base + [
            "-b", str(baud),
            "--before", "default-reset",
            "--after", "hard-reset",
            "write-flash",
            "--flash-mode", "dio",
            "--flash-size", "8MB",
            "--flash-freq", "80m",
            "0x0", IMAGE,
        ]
        rc = run(cmd, log)
        if rc == 0:
            log("\n烧录成功。")
            if not args.no_monitor:
                time.sleep(1.5)
                log("开始抓串口日志...\n")
                mon = [
                    sys.executable, os.path.join(HERE, "serial_log.py"),
                    "-p", port, "-s", "20",
                ]
                run(mon, log)
            return 0
        log("第 %d 次失败（baud=%d），等待端口重新出现..." % (attempt, baud))
        port = wait_port(port, 30, log)
        if port is None:
            log("端口又没了，放弃")
            return 1

    log("重试 %d 次仍失败" % args.tries)
    return 1


if __name__ == "__main__":
    sys.exit(main())

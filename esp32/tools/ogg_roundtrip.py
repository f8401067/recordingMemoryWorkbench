#!/usr/bin/env python3
"""把参考 .ogg 里的 Opus 音频包抽出来，打成 [u32 len][bytes] 序列。

用于 tools/test_ogg_mux.c 的往返验证：
    参考文件 → 抽包 → 用固件的 ogg_mux.c 重新封装 → ffprobe / ffmpeg 对比

    python3 tools/ogg_roundtrip.py ref.ogg packets.bin
"""
import struct
import sys


def parse_pages(data):
    off = 0
    pages = []
    while off + 27 <= len(data):
        if data[off:off + 4] != b"OggS":
            raise SystemExit("不是 Ogg 流：offset %d" % off)
        nseg = data[off + 26]
        segs = data[off + 27:off + 27 + nseg]
        payload_at = off + 27 + nseg
        payload_len = sum(segs)
        pages.append({
            "granule": struct.unpack("<q", data[off + 6:off + 14])[0],
            "payload": data[payload_at:payload_at + payload_len],
            "segs": list(segs),
        })
        off = payload_at + payload_len
    return pages


def split_packets(pages):
    pkts = []
    cur = b""
    for p in pages:
        pos = 0
        for lace in p["segs"]:
            cur += p["payload"][pos:pos + lace]
            pos += lace
            if lace < 255:
                pkts.append(cur)
                cur = b""
    if cur:
        pkts.append(cur)
    return pkts


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    data = open(sys.argv[1], "rb").read()
    pkts = split_packets(parse_pages(data))
    print("总包数 %d，头部 %s / %s" % (len(pkts), pkts[0][:8], pkts[1][:8]))

    audio = pkts[2:]
    sizes = [len(p) for p in audio]
    print("音频包 %d 个，大小 %d~%d 字节，平均 %.1f" %
          (len(audio), min(sizes), max(sizes), sum(sizes) / len(sizes)))

    with open(sys.argv[2], "wb") as f:
        for p in audio:
            f.write(struct.pack("<I", len(p)))
            f.write(p)
    print("已写出", sys.argv[2])


if __name__ == "__main__":
    main()

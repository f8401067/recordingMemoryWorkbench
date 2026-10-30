/* 在电脑上验证 Ogg 封装器：把一串 Opus 包重新封成 .ogg，交给 ffmpeg 检查
 *
 *   1) 用 ffmpeg 造一段参考音频（16kHz 单声道 Opus）
 *   2) tools/ogg_roundtrip.py 把参考文件里的 Opus 包抽出来
 *   3) 本程序用固件同一份 ogg_mux.c 重新封装
 *   4) ffprobe 对比时长、ffmpeg 解码对比采样数
 *
 * 编译：cc -O2 -I../main test_ogg_mux.c ../main/ogg_mux.c -o /tmp/test_ogg_mux
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "ogg_mux.h"

int main(int argc, char **argv)
{
    if (argc < 3) {
        fprintf(stderr, "usage: %s packets.bin out.ogg\n", argv[0]);
        return 2;
    }
    FILE *in = fopen(argv[1], "rb");
    if (!in) {
        perror("open packets");
        return 1;
    }
    FILE *out = fopen(argv[2], "wb");
    if (!out) {
        perror("open out");
        return 1;
    }

    static ogg_mux_t mux;
    ogg_mux_init(&mux, 1, 16000, 312, 960);
    if (!ogg_mux_begin(&mux, out, 0x5EC0DE01u)) {
        fprintf(stderr, "begin failed\n");
        return 1;
    }

    int n = 0;
    uint8_t lenbuf[4];
    uint8_t buf[4096];
    while (fread(lenbuf, 1, 4, in) == 4) {
        uint32_t len = (uint32_t)lenbuf[0] | ((uint32_t)lenbuf[1] << 8) | ((uint32_t)lenbuf[2] << 16) |
                       ((uint32_t)lenbuf[3] << 24);
        if (len == 0 || len > sizeof(buf)) {
            fprintf(stderr, "bad packet len %u\n", len);
            return 1;
        }
        if (fread(buf, 1, len, in) != len) {
            break;
        }
        if (!ogg_mux_add(&mux, buf, len)) {
            fprintf(stderr, "add packet %d failed\n", n);
            return 1;
        }
        n++;
    }
    ogg_mux_end(&mux);
    fclose(in);
    fclose(out);
    printf("packets=%d bytes=%u\n", n, mux.total_bytes);
    return 0;
}

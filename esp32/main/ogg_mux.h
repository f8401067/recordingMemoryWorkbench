/* 极简 Ogg 封装器（纯 C，只依赖 stdio）
 *
 * 只做一件事：把一串 Opus 包（每包固定时长）封成 Ogg 流。
 *   page1: OpusHead（BOS）
 *   page2: OpusTags
 *   page3..: 音频页，每页最多 100 个包
 *
 * 之所以单独拆出来：这段逻辑最容易写错（CRC、lacing、granule），
 * 拆出来就能在电脑上直接跑一遍再用 ffmpeg 验证，见 tools/test_ogg_mux.c
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

#define OGG_MAX_PAYLOAD 4096  /* 一页音频数据上限，12kbps 下 2 秒才 ~3KB */
#define OGG_MAX_SEGS 255
#define OGG_PACKETS_PER_PAGE 100 /* 100 × 20ms = 2 秒一页，够紧凑又好恢复 */

typedef struct {
    FILE *fp;
    uint32_t serial;
    uint32_t page_seq;
    uint8_t page[OGG_MAX_PAYLOAD];
    uint32_t page_len;
    uint8_t segs[OGG_MAX_SEGS];
    int nseg;
    uint64_t granule;          /* 已写入的 48kHz 采样数（不含 pre-skip） */
    uint32_t total_bytes;
    uint32_t samples_per_pkt;  /* 每个包对应的 48kHz 采样数 */
    uint16_t pre_skip;
    uint16_t channels;
    uint32_t input_rate;
    bool started;
    bool finished;
} ogg_mux_t;

void ogg_mux_init(ogg_mux_t *m, uint16_t channels, uint32_t input_rate, uint16_t pre_skip,
                  uint32_t samples_per_pkt);

/* 写 OpusHead / OpusTags 两页，之后才能 add */
bool ogg_mux_begin(ogg_mux_t *m, FILE *fp, uint32_t serial);

/* 追加一个 Opus 包（通常 20ms，几十字节） */
bool ogg_mux_add(ogg_mux_t *m, const uint8_t *pkt, uint32_t len);

/* 收尾：刷出最后一页并打 EOS 标记 */
bool ogg_mux_end(ogg_mux_t *m);

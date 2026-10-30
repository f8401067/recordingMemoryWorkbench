/* Opus 编码 + Ogg 封装
 *
 * 16kHz 单声道、20ms 一帧、默认 12kbps：
 *   1 分钟 ≈ 90KB，比 64kbps 的 WAV/ADPCM 小 5 倍，
 *   服务端 ffmpeg 直接能解，转写链路不用改。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

#include "app_config.h"
#include "esp_err.h"
#include "ogg_mux.h"

/* OpusHead 里的 pre-skip：编码器固定延迟 6.5ms（48kHz 下 312 个采样） */
#define OGG_PRE_SKIP 312
/* 每个包对应的 48kHz 采样数（20ms = 960） */
#define OGG_SAMPLES_PER_PKT (APP_FRAME_MS * 48)

typedef struct {
    ogg_mux_t mux;
    int16_t pcm[APP_FRAME_SAMPLES];
    int pcm_fill;
    bool active;
} ogg_opus_rec_t;

/* 编码器全局初始化（整个固件只调一次） */
esp_err_t ogg_opus_init(void);
void ogg_opus_deinit(void);

/* 往一个已打开（可写）的文件写 Ogg Opus 头，之后就能不断 write */
esp_err_t ogg_opus_begin(ogg_opus_rec_t *r, FILE *fp);

/* 送 PCM（16bit 单声道 16kHz），内部凑满 20ms 才编码 */
void ogg_opus_write(ogg_opus_rec_t *r, const int16_t *pcm, int n);

/* 收尾：补齐不足一帧、写 EOS 页、flush。返回写入的总字节数 */
uint32_t ogg_opus_end(ogg_opus_rec_t *r);

static inline uint32_t ogg_opus_bytes(const ogg_opus_rec_t *r)
{
    return r->mux.total_bytes;
}

/* 已经写进文件的音频时长（毫秒），按 granule 精确算，不是墙钟时间 */
static inline uint32_t ogg_opus_ms(const ogg_opus_rec_t *r)
{
    return (uint32_t)(r->mux.granule * 1000ULL / 48000ULL);
}

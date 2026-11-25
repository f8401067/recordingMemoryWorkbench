/* 人声检测：判据交给 ESP-SR 的 WebRTC VAD，起判/拖尾/统计仍在这里控制
 *
 * 和以前的手写版本相比：
 *   - 以前：能量 > 噪声底×2.6 且过零率 < 0.42 → 只认"响不响、高频多不多"
 *   - 现在：ESP-SR 的 VAD（WebRTC 算法）直接判"这帧像不像人声"，
 *     对敲击、摩擦、风扇、键盘这些的区分度好得多
 * 对外语义保持不变：speech = 是否处于一段语音中（含拖尾），
 * last_above = 本帧是否真被判为人声（拖尾帧为 false，用于统计有效时长）。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

typedef struct {
    void *sr;         /* ESP-SR VAD 句柄（sr_vad_t） */
    float last_rms;   /* 上一帧 RMS，给电平条用 */
    float peak_rms;
    /* 自适应噪声底 + 本帧实际生效的能量闸门（调试/日志用） */
    float noise_rms;
    float last_gate;
    int speech_run;   /* 连续人声帧数（起判用） */
    int silence_run;  /* 连续静音帧数（拖尾用） */
    bool speech;      /* 是否处于一段语音中（含拖尾） */
    bool last_above;  /* 本帧是否真被判为人声 */
    uint32_t voiced_ms;
    uint32_t half_ms;
    uint32_t total_ms;
    uint32_t frame_ms;
} vad_t;

void mvad_init(vad_t *v, uint32_t frame_ms);

/* 处理一帧 16bit 单声道 16kHz PCM（320 采样 = 20ms），返回本帧是否处于人声段 */
bool mvad_process(vad_t *v, const int16_t *pcm, int n);

/* 0~100 的显示电平（对数刻度） */
int mvad_level(const vad_t *v);

void mvad_reset_segment(vad_t *v);

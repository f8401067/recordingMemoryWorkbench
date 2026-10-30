/* 轻量人声检测：短时能量 + 过零率 + 自适应噪声底 + 拖尾 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

typedef struct {
    float noise_floor;   /* 自适应噪声底（RMS） */
    float last_rms;      /* 上一帧 RMS，给电平条用 */
    float last_zcr;
    float peak_rms;      /* 整段录音的峰值，决定要不要上传的一个辅助量 */
    int speech_run;      /* 连续超阈值帧数 */
    int silence_run;     /* 连续低于阈值帧数 */
    bool speech;         /* 当前是否处于语音段（含拖尾） */
    uint32_t voiced_ms;  /* 真正超阈值的累计时长 */
    uint32_t half_ms;    /* 静音但仍在拖尾的累计时长，不算进 voiced */
    uint32_t total_ms;
    uint32_t frame_ms;
} vad_t;

void vad_init(vad_t *v, uint32_t frame_ms);

/* 处理一帧 16bit 单声道 PCM，返回本帧是否处于人声段 */
bool vad_process(vad_t *v, const int16_t *pcm, int n);

/* 0~100 的显示电平（对数刻度，更贴近观感） */
int vad_level(const vad_t *v);

void vad_reset_segment(vad_t *v);

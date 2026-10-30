/* ES8311 编解码芯片采集（正点原子 BOX0 这类一体板）
 *
 * ES8311 一颗芯片同时管麦克风（ADC）和喇叭（DAC），
 * 走 esp_codec_dev 官方驱动：I2C 配寄存器，I2S 收数据。
 * 数据格式 16bit / 16kHz，开发时双开通道，实际只读左声道（咪头）。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

esp_err_t audio_init(void);

/* 打开/关闭编解码器。不开麦时关掉，省电 */
esp_err_t audio_start(void);
esp_err_t audio_stop(void);

/* 读最多 max_frames 个 16bit 采样，返回实际帧数（0 表示超时/出错）。
 * 内部已做位宽对齐和直流阻断。 */
int audio_read(int16_t *dst, int max_frames, uint32_t timeout_ms);

/* 麦克风电源开关（接了 MOSFET 才有意义，否则是空操作） */
void audio_mic_power(bool on);

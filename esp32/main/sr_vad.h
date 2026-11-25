/* ESP-SR VAD 的薄封装
 *
 * 为什么需要这一层：ESP-SR 的接口叫 `vad_process()`，和我们自己的
 * `vad_process()` 同名，放在同一个编译单元里会声明冲突。这里把 esp_vad.h
 * 的符号关在一个 .c 里，对外只暴露 sr_vad_* 几个函数。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

typedef struct sr_vad_s *sr_vad_t;

/* mode: 0=Normal 1=Aggressive 2=VeryAggressive 3/4 更激进（更不容易判成人声） */
sr_vad_t sr_vad_create(int mode);

/* 送一帧 16kHz / 20ms / 320 采样的 PCM，返回 true 表示本帧有人声 */
bool sr_vad_run(sr_vad_t h, int16_t *pcm);

void sr_vad_destroy(sr_vad_t h);

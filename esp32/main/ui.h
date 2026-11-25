/* 按键 / LED / 蜂鸣器：有屏无屏都用同一套状态反馈
 *
 * 长按 0.7 秒开始录音，再长按一次结束。
 * 无屏幕时靠声音区分：
 *   开始 → 两声短促「嘀嘀」
 *   结束 → 一声长「嘀——」
 *   上传成功 → 三声「嘀嘀嘀」
 *   出错 → 一声很长的低音
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

typedef enum {
    UI_EV_NONE = 0,
    UI_EV_SHORT,  /* 单击：暂时不用 */
    UI_EV_DOUBLE, /* 双击：开始 / 结束录音 */
    UI_EV_TRIPLE, /* 三击：进配网（不清凭据） */
    UI_EV_LONG,   /* 长按 0.7 秒：停止录音并回到待机 */
    UI_EV_RESET,  /* 长按 3 秒：清除配网信息并重启进配网 */
} ui_event_t;

typedef enum {
    SND_NONE = 0,
    SND_START,
    SND_STOP,
    SND_OK,
    SND_ERR,
} ui_sound_t;

typedef enum {
    LED_OFF = 0,
    LED_ON,
    LED_BLINK_SLOW,
    LED_BLINK_FAST,
    LED_DIM, /* 微亮：暂停状态用作"还活着但没在录"的提示 */
} ui_led_mode_t;

esp_err_t ui_init(void);

/* 每 10~20ms 调一次 */
ui_event_t ui_poll(uint32_t now_ms);

/* 每 10~20ms 调一次：驱动 LED 闪烁和蜂鸣器节奏 */
void ui_tick(uint32_t now_ms);

void ui_led(ui_led_mode_t mode);
void ui_sound(ui_sound_t s);

/* 没有屏幕时用蜂鸣器把 6 位编号"念"出来（阻塞，只在开机时调） */
void ui_announce_code(const char *code);

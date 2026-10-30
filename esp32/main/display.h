/* 屏幕：128x64 单色画布 + 三种驱动（无屏 / SSD1306 I2C / ST7735 SPI）
 *
 * 逻辑分辨率固定 128x64，换屏只影响 flush 的实现，界面代码不用改。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

typedef enum {
    SCR_BOOT = 0,
    SCR_PAIR,
    SCR_PROV, /* 配网中：告诉用户连哪个热点、开哪个地址 */
    SCR_READY,
    SCR_REC,
    SCR_SAVE,
    SCR_UPLOAD,
    SCR_DONE,
    SCR_SKIP,
    SCR_ERR,
} screen_kind_t;

typedef struct {
    screen_kind_t kind;
    char title[20];  /* 左上角状态词：REC / READY / SEND / ERR */
    char line[40];   /* 中间提示，自动大写 */
    char hint[44];   /* 底部提示 */
    char code[8];    /* 配对编号，右侧显示 */
    uint32_t secs;   /* 录音计时 */
    int level;       /* 0~100 麦电平 */
    int step;        /* 上传进度：第几个 */
    int steps;       /* 一共几个 */
    bool blink;      /* 录音红点闪烁相位 */
} screen_model_t;

esp_err_t display_init(void);
bool display_available(void);
void display_render(const screen_model_t *m);
void display_power(bool on);

/* 320x240 彩色屏的排版实现在 lcd_ui.c，display.c 只做转发 */
void lcd_ui_render(const screen_model_t *m);

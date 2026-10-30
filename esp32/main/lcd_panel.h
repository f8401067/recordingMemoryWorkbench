/* ST7789V2 320x240 SPI 屏：面板驱动 + 绘图原语
 *
 * 用一个 320x240x2 的 PSRAM 帧缓冲做全屏合成，推屏时按 40 行一条
 * 拷进内部 RAM 再交给 SPI DMA（PSRAM 直接当 DMA 源在 S3 上不稳，
 * 显式拷一次最省心）。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "app_config.h"

#ifndef BOARD_LCD_W
#define BOARD_LCD_W 320
#define BOARD_LCD_H 240
#endif

#define LCD_W BOARD_LCD_W
#define LCD_H BOARD_LCD_H

/* RGB565 */
#define LCD_BLACK 0x0000
#define LCD_WHITE 0xFFFF

esp_err_t lcd_init(void);
bool lcd_ready(void);

/* 推屏：把帧缓冲刷到面板 */
void lcd_present(void);

void lcd_clear(uint16_t c);
void lcd_fill(int x, int y, int w, int h, uint16_t c);
void lcd_frame(int x, int y, int w, int h, uint16_t c, int thickness);

/* 文字：内置 5x7 点阵，scale 是整数放大倍数。
 * bg 传 -1 表示不改底色（透明）。 */
void lcd_text(int x, int y, int scale, uint16_t fg, int32_t bg, const char *s);
void lcd_text_center(int y, int scale, uint16_t fg, int32_t bg, const char *s);
void lcd_text_right(int x_right, int y, int scale, uint16_t fg, int32_t bg, const char *s);

int lcd_text_w(const char *s, int scale);
/* 挑一个放得下的缩放倍数：从 max_scale 往下退，最小 1 */
int lcd_fit_scale(const char *s, int max_w, int max_scale);

/* 圆点（录音指示灯用） */
void lcd_dot(int cx, int cy, int r, uint16_t c);

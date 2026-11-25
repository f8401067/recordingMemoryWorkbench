/* 1.54" 电子墨水屏 200x200 单色（SSD1681 类控制器，SPI）
 *
 * 和彩色屏最大的三点不同：
 *   1. 只有黑白两色，1 个像素 1 bit，整屏 5000 字节；
 *   2. 刷新很慢（局部约 0.4 秒，全刷约 2 秒），绝不能在主循环里等；
 *   3. 断电后画面依然保留，所以空闲/深睡时可以直接把屏的电断掉。
 *
 * 因此这里用一个后台任务专门做 SPI 传输和等 BUSY，
 * epd_present() 只是把帧缓冲交出去就返回，录音线程不会被拖住。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "app_config.h"
#include "esp_err.h"

#ifndef BOARD_LCD_W
#define BOARD_LCD_W 200
#define BOARD_LCD_H 200
#endif

#define EPD_W BOARD_LCD_W
#define EPD_H BOARD_LCD_H

/* 1=白（对应 bit 置 1），0=黑 */
#define EPD_WHITE 1
#define EPD_BLACK 0

esp_err_t epd_init(void);
bool epd_ready(void);

/* 把帧缓冲交给后台任务刷新。内容没变就不会真的刷。 */
void epd_present(void);
/* 关键画面（配网页 / 设备码）用：提交后一直等到刷完 */
void epd_present_blocking(void);

/* 绘图原语（写的是后台缓冲，不直接下屏） */
void epd_clear(uint8_t c);
void epd_fill(int x, int y, int w, int h, uint8_t c);
void epd_frame(int x, int y, int w, int h, uint8_t c, int thickness);
void epd_hline(int x, int y, int w, uint8_t c);
void epd_dot(int cx, int cy, int r, uint8_t c);

/* 5x7 点阵，scale 为整数放大倍数；bg 传 -1 表示不改底色 */
void epd_text(int x, int y, int scale, uint8_t fg, int32_t bg, const char *s);
void epd_text_center(int y, int scale, uint8_t fg, int32_t bg, const char *s);
void epd_text_right(int x_right, int y, int scale, uint8_t fg, int32_t bg, const char *s);
int epd_text_w(const char *s, int scale);
int epd_fit_scale(const char *s, int max_w, int max_scale);

/* 断电：画面保留；恢复时调 epd_resume() 重新初始化面板（会全刷一次当前画面） */
void epd_power_off(void);
esp_err_t epd_resume(void);

/* 调试：依次刷全白/全黑/竖条纹/横条纹，用来判断是数据错还是驱动没起来 */
void epd_selftest(void);

/* 彩色屏界面：和单色版共用同一个 screen_model_t。
 * 分辨率由板级给（BOX0 是 240x240 方屏），排版全部按比例算。
 */
#include <stdio.h>
#include <string.h>

#include "display.h"
#include "lcd_panel.h"

/* RGB565 配色 */
#define C_BG 0x0882   /* #101418 深底 */
#define C_CARD 0x1925 /* #1E2630 卡片/顶栏 */
#define C_FG 0xFFFF   /* 白 */
#define C_DIM 0x8CB4  /* #8C94A0 次要文字 */
#define C_ACC 0x1656  /* #12C8B0 青绿，主色 */
#define C_WARN 0xFD40 /* #FFA800 琥珀 */
#define C_BAD 0xFA6A  /* #FF5050 红 */
#define C_BAR 0x3186  /* 进度条底色 */

/* 方屏小尺寸时整体缩一档，字不挤 */
#if LCD_W < 300
#define T_TITLE 2
#define T_BIG 5
#define T_MID 3
#define T_SMALL 2
#define TOP_H 40
#else
#define T_TITLE 3
#define T_BIG 7
#define T_MID 4
#define T_SMALL 2
#define TOP_H 52
#endif

static void top_bar(const screen_model_t *m, uint16_t accent)
{
    lcd_fill(0, 0, LCD_W, TOP_H, C_CARD);
    lcd_fill(0, TOP_H - 2, LCD_W, 2, accent);
    lcd_text(10, (TOP_H - 2 - 7 * T_TITLE) / 2, T_TITLE, C_FG, -1, m->title);
    if (m->code[0] && m->kind != SCR_PROV) {
        /* 配网页的编号已经写在热点名里了，右上角不重复 */
        lcd_text_right(LCD_W - 10, (TOP_H - 2 - 7 * T_SMALL) / 2, T_SMALL, C_DIM, -1, m->code);
    }
}

static void footer(const char *s, uint16_t color)
{
    if (s && s[0]) {
        int sc = lcd_fit_scale(s, LCD_W - 20, T_SMALL);
        lcd_text_center(LCD_H - 7 * sc - 8, sc, color, -1, s);
    }
}

/* 大字 + 一行说明 + 一行提示，DONE/SKIP/ERR 共用 */
static void word_screen(const screen_model_t *m, const char *word, uint16_t color)
{
    int sc = lcd_fit_scale(word, LCD_W - 40, T_BIG);
    lcd_text_center(58, sc, color, -1, word);
    if (m->line[0]) {
        int s2 = lcd_fit_scale(m->line, LCD_W - 32, T_MID);
        lcd_text_center(58 + 7 * sc + 22, s2, C_FG, -1, m->line);
    }
    footer(m->hint, C_DIM);
}

void lcd_ui_render(const screen_model_t *m)
{
    lcd_clear(C_BG);

    switch (m->kind) {
    case SCR_PROV: {
        /* 这块是用户第一眼要看的：连哪个热点 → 打开哪个地址 */
        top_bar(m, C_ACC);
        int y = TOP_H + 12;
        lcd_text_center(y, T_SMALL, C_DIM, -1, "CONNECT TO WIFI");
        y += 7 * T_SMALL + 10;

        const int cx = 14, cw = LCD_W - 28, ch = 7 * T_BIG + 16;
        lcd_fill(cx, y, cw, ch, C_CARD);
        lcd_frame(cx, y, cw, ch, C_ACC, 2);
        const char *ssid = m->line[0] ? m->line : "RECALL-000000";
        int sc = lcd_fit_scale(ssid, cw - 24, T_BIG);
        lcd_text_center(y + (ch - 7 * sc) / 2, sc, C_ACC, -1, ssid);
        y += ch + 12;

        lcd_text_center(y, T_SMALL, C_DIM, -1, "OPEN IN BROWSER");
        y += 7 * T_SMALL + 6;
        const char *ip = m->hint[0] ? m->hint : "192.168.4.1";
        int s2 = lcd_fit_scale(ip, LCD_W - 32, T_MID);
        lcd_text_center(y, s2, C_FG, -1, ip);
        footer("2.4G WIFI ONLY", C_DIM);
        break;
    }

    case SCR_REC: {
        top_bar(m, C_BAD);
        char t[10];
        unsigned secs = m->secs > 99 * 60 + 59 ? 99 * 60 + 59 : m->secs;
        snprintf(t, sizeof(t), "%02u:%02u", secs / 60, secs % 60);
        int sc = T_BIG;
        lcd_dot(LCD_W / 2 - (int)lcd_text_w(t, sc) / 2 - 22, TOP_H + 24 + 7 * sc / 2, 11, m->blink ? C_BAD : C_CARD);
        lcd_text_center(TOP_H + 24, sc, C_FG, -1, t);

        /* 电平条 */
        const int cells = 20, bw = 8, gap = 1;
        int total = cells * (bw + gap) - gap;
        int bx = (LCD_W - total) / 2;
        int by = TOP_H + 24 + 7 * sc + 18;
        int on = m->level * cells / 100;
        for (int i = 0; i < cells; i++) {
            uint16_t c = C_BAR;
            if (i < on) {
                c = (m->level > 85) ? C_BAD : C_ACC;
            }
            lcd_fill(bx + i * (bw + gap), by, bw, 14, c);
        }
        footer(m->hint, C_DIM);
        break;
    }

    case SCR_UPLOAD: {
        top_bar(m, C_ACC);
        lcd_text_center(TOP_H + 12, T_BIG, C_FG, -1, m->line[0] ? m->line : "UPLOAD");

        const int bw = LCD_W - 40, bh = 18;
        int bx = 20, by = TOP_H + 12 + 7 * T_BIG + 16;
        lcd_frame(bx, by, bw, bh, C_ACC, 2);
        if (m->steps > 0) {
            int done = (bw - 6) * m->step / m->steps;
            lcd_fill(bx + 3, by + 3, done, bh - 6, C_ACC);
        }
        char buf[16];
        snprintf(buf, sizeof(buf), "%d/%d", m->step, m->steps);
        lcd_text_center(by + bh + 10, T_MID, C_DIM, -1, buf);
        footer(m->hint, C_DIM);
        break;
    }

    case SCR_DONE:
        top_bar(m, C_ACC);
        word_screen(m, "DONE", C_ACC);
        break;

    case SCR_SKIP:
        top_bar(m, C_WARN);
        word_screen(m, "SKIP", C_WARN);
        break;

    case SCR_ERR:
        top_bar(m, C_BAD);
        word_screen(m, "ERROR", C_BAD);
        break;

    case SCR_PAIR: {
        top_bar(m, C_WARN);
        int y = TOP_H + 12;
        lcd_text_center(y, T_SMALL, C_DIM, -1, "DEVICE CODE");
        y += 7 * T_SMALL + 10;
        if (m->code[0]) {
            int sc = lcd_fit_scale(m->code, LCD_W - 40, T_BIG);
            lcd_text_center(y, sc, C_FG, -1, m->code);
            y += 7 * sc + 14;
        }
        if (m->line[0]) {
            uint16_t c = (strcmp(m->line, "DEVICE") == 0) ? C_ACC : C_WARN;
            lcd_text_center(y, T_MID, c, -1, m->line);
        }
        footer(m->hint[0] ? m->hint : "ADD IN WEB UI", C_DIM);
        break;
    }

    case SCR_READY:
    default: {
        top_bar(m, C_ACC);
        int y = TOP_H + 18;
        lcd_text_center(y, T_BIG, C_FG, -1, "HOLD 0.7S");
        y += 7 * T_BIG + 12;
        lcd_text_center(y, T_MID, C_ACC, -1, "TO RECORD");
        y += 7 * T_MID + 14;
        if (m->line[0]) {
            lcd_text_center(y, T_SMALL, C_DIM, -1, m->line);
            y += 7 * T_SMALL + 8;
        }
        if (m->hint[0]) {
            uint16_t c = (strcmp(m->hint, "PAIRED") == 0) ? C_ACC : C_WARN;
            lcd_text_center(y, T_MID, c, -1, m->hint);
        }
        break;
    }
    }

    lcd_present();
}

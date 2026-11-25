/* 墨水屏界面：和彩色屏共用同一个 screen_model_t
 *
 * 电子纸的两个约束决定了这里的排版和彩色屏不一样：
 *   1. 只有黑白，不能靠颜色区分主次，靠字号和框线；
 *   2. 刷新一次要几百毫秒且屏幕会闪，所以凡是"会动"的东西都要压掉：
 *      录音计时按 10 秒量化、电平条和闪烁点一律不画（实时反馈交给 LED）。
 */
#include <stdio.h>
#include <string.h>

#include "display.h"
#include "epd_panel.h"

/* 200x200 上刚好够用的三档字号 */
#define T_TITLE 3 /* 顶栏（放大一档，200px 屏上才看得清） */
#define T_BIG 4   /* 设备码 / 大字状态 */
#define T_MID 3   /* 次要大字 */
#define T_SMALL 2 /* 正文 */
#define TOP_H 44  /* 顶栏两行：状态 + 日期，给大字号留位置 */

static void top_bar(const screen_model_t *m)
{
    epd_fill(0, 0, EPD_W, TOP_H, EPD_BLACK);
    epd_text(6, 3, T_TITLE, EPD_WHITE, -1, m->title);

    /* 右上角：电量。字号用 T_SMALL(2)，比原来的 1 大一倍，隔远了也看得清。
     * 设备编号只在配对页中间用大字显示，顶栏不再挤它。 */
    if (m->batt >= 0) {
        char b[12];
        snprintf(b, sizeof(b), "%d%%", m->batt);
        epd_text_right(EPD_W - 8, 3 + (7 * T_TITLE - 7 * T_SMALL) / 2, T_SMALL, EPD_WHITE, -1, b);
    }
    /* 第二行：年月日。时间没校准过就空着，等联网对时后下一次刷屏自然出现 */
    if (m->date[0]) {
        epd_text(6, 26, T_SMALL, EPD_WHITE, -1, m->date);
    }
}

static void footer(const char *s)
{
    if (s && s[0]) {
        int sc = epd_fit_scale(s, EPD_W - 16, T_SMALL);
        epd_text_center(EPD_H - 7 * sc - 8, sc, EPD_BLACK, -1, s);
    }
}

/* 居中的一行，返回下一行的 y。间距 10px：200 宽的小屏上一行贴一行会挤在一起 */
static int cline(int y, int sc, const char *s)
{
    epd_text_center(y, sc, EPD_BLACK, -1, s);
    return y + 7 * sc + 10;
}

/* 按剩余宽度选字号；墨水屏字体最小只有 1 档，超长 SSID（最长 32 字节）
 * 光靠缩字号也放不下，再从头截断，保证一定不会顶出屏幕。 */
static int fit_copy(char *dst, size_t cap, const char *src, int max_w, int max_scale)
{
    int sc = epd_fit_scale(src, max_w, max_scale);
    snprintf(dst, cap, "%s", src);
    while (dst[0] && epd_text_w(dst, sc) > max_w) {
        dst[strlen(dst) - 1] = '\0';
    }
    return sc;
}

/* 大字 + 两行说明 + 底部提示，DONE / SKIP / ERR 共用 */
static void word_screen(const screen_model_t *m, const char *word)
{
    int y = TOP_H + 16;
    int sc = epd_fit_scale(word, EPD_W - 40, T_BIG);
    y = cline(y, sc, word);
    if (m->line[0]) {
        int s2 = epd_fit_scale(m->line, EPD_W - 20, T_MID);
        y = cline(y, s2, m->line);
    }
    footer(m->hint);
}

void epd_ui_render(const screen_model_t *m)
{
    epd_clear(EPD_WHITE);

    switch (m->kind) {
    case SCR_PROV: {
        /* 用户第一眼要看的：连哪个热点 → 打开哪个地址。
         * 起始 y 留一点空隙，让整块内容在 200 高的屏上大致居中 */
        top_bar(m);
        int y = TOP_H + 18;
        y = cline(y, T_SMALL, "CONNECT TO WIFI");

        const int cx = 10, cw = EPD_W - 20, ch = 7 * T_MID + 14;
        epd_frame(cx, y, cw, ch, EPD_BLACK, 2);
        const char *ssid = m->line[0] ? m->line : "RECALL-000000";
        int sc = epd_fit_scale(ssid, cw - 16, T_MID);
        epd_text_center(y + (ch - 7 * sc) / 2, sc, EPD_BLACK, -1, ssid);
        y += ch + 12;

        y = cline(y, T_SMALL, "OPEN IN BROWSER");
        const char *ip = m->hint[0] ? m->hint : "192.168.4.1";
        int s2 = epd_fit_scale(ip, EPD_W - 24, T_BIG);
        y = cline(y, s2, ip);
        footer("2.4G WIFI ONLY");
        break;
    }

    case SCR_PAIR: {
        top_bar(m);
        int y = TOP_H + 12;
        y = cline(y, T_SMALL, "DEVICE CODE");
        if (m->code[0]) {
            int sc = epd_fit_scale(m->code, EPD_W - 24, T_BIG);
            y = cline(y, sc, m->code);
        }
        if (m->line[0]) {
            int s2 = epd_fit_scale(m->line, EPD_W - 20, T_MID);
            y = cline(y, s2, m->line);
        }
        footer(m->hint[0] ? m->hint : "ADD IN WEB UI");
        break;
    }

    case SCR_REC: {
        top_bar(m);
        /* 不显示计时（常驻模式下一整天都在录，秒数没有意义，还逼着屏幕刷新）。
         * 这里只给一句状态，画面整个录音期间保持不变。 */
        int y = TOP_H + 26;
        int sc = epd_fit_scale("RECORDING", EPD_W - 40, T_BIG);
        int tw = epd_text_w("RECORDING", sc);
        int tx = (EPD_W - tw) / 2;
        /* 实心圆点表示"在录"，不做闪烁（闪烁会触发连续刷新） */
        epd_dot(tx - 14, y + 7 * sc / 2, 6, EPD_BLACK);
        epd_text(tx, y, sc, EPD_BLACK, -1, "RECORDING");
        y += 7 * sc + 16;
        /* 现在结束录音是"双击"，不是长按 */
        const char *stop = m->line[0] ? m->line : "DOUBLE TAP: STOP";
        y = cline(y, epd_fit_scale(stop, EPD_W - 20, T_MID), stop);
        if (m->note[0]) {
            y = cline(y, T_SMALL, m->note);
        }
        footer(m->hint[0] ? m->hint : "HOLD 3S: RESET WIFI");
        break;
    }

    case SCR_LISTEN: {
        top_bar(m);
        int y = TOP_H + 22;
        y = cline(y, epd_fit_scale("LISTENING", EPD_W - 24, T_BIG), "LISTENING");
        y = cline(y, T_MID, m->line[0] ? m->line : "SPEAK ANYTIME");
        if (m->note[0]) {
            y = cline(y, T_SMALL, m->note);
        }
        footer(m->hint[0] ? m->hint : "HOLD 3S: RESET WIFI");
        break;
    }

    case SCR_PAUSED: {
        top_bar(m);
        int y = TOP_H + 22;
        y = cline(y, epd_fit_scale("PAUSED", EPD_W - 24, T_BIG), "PAUSED");
        y = cline(y, T_MID, m->line[0] ? m->line : "MIC + REC OFF");
        if (m->note[0]) {
            y = cline(y, T_SMALL, m->note);
        }
        footer(m->hint[0] ? m->hint : "PWR: RESUME");
        break;
    }

    case SCR_UPLOAD: {
        top_bar(m);

        /* 状态行：SENDING / SENT / SEND FAIL。字号按宽度自适应，
         * 不再固定用大字，避免长状态被 200px 宽的屏裁掉 */
        int y = TOP_H + 6;
        const char *note = m->note[0] ? m->note : "SENDING";
        y = cline(y, epd_fit_scale(note, EPD_W - 20, T_SMALL), note);

        /* 连上的 WiFi 名称单独一个框：字号自适应 + 超长截断，
         * 最长 32 字节的 SSID 也保证能完整落在屏幕内 */
        const char *ssid = m->line[0] ? m->line : "NO WIFI";
        char shown[40];
        int sc = fit_copy(shown, sizeof(shown), ssid, EPD_W - 36, T_MID);
        const int cx = 10, cw = EPD_W - 20, ch = 7 * sc + 14;
        epd_frame(cx, y, cw, ch, EPD_BLACK, 2);
        epd_text_center(y + (ch - 7 * sc) / 2, sc, EPD_BLACK, -1, shown);
        y += ch + 10;

        /* 还没数清有几个文件时（刚双击、上传任务还没列目录）不画进度条，
         * 免得屏上出现一个永远不动的 "0/0" */
        if (m->steps > 0) {
            const int bw = EPD_W - 40, bh = 14;
            int bx = 20;
            epd_frame(bx, y, bw, bh, EPD_BLACK, 2);
            int done = (bw - 6) * m->step / m->steps;
            epd_fill(bx + 3, y + 3, done, bh - 6, EPD_BLACK);
            y += bh + 8;
            char buf[16];
            snprintf(buf, sizeof(buf), "%d/%d", m->step, m->steps);
            cline(y, epd_fit_scale(buf, EPD_W - 40, T_MID), buf);
        }
        footer(m->hint[0] ? m->hint : "KEEP POWER ON");
        break;
    }

    case SCR_DONE:
        top_bar(m);
        word_screen(m, "DONE");
        break;

    case SCR_SKIP:
        top_bar(m);
        word_screen(m, "SKIP");
        break;

    case SCR_ERR:
        top_bar(m);
        word_screen(m, "ERROR");
        break;

    case SCR_BOOT:
        top_bar(m);
        word_screen(m, "RECALL");
        break;

    case SCR_OFF: {
        /* 关机前的一屏。墨水屏断电后画面还在，所以用户一眼就能看出是关着的 */
        epd_clear(EPD_BLACK);
        int sc = epd_fit_scale("OFF", EPD_W - 24, 12);
        int w = epd_text_w("OFF", sc);
        epd_text((EPD_W - w) / 2, (EPD_H - 7 * sc) / 2 - 12, sc, EPD_WHITE, -1, "OFF");
        const char *tip = "PRESS PWR TO START";
        int s2 = epd_fit_scale(tip, EPD_W - 12, 2);
        epd_text_center(EPD_H - 7 * s2 - 10, s2, EPD_WHITE, -1, tip);
        break;
    }

    case SCR_READY:
    default: {
        top_bar(m);
        int y = TOP_H + 20;
        int sc = epd_fit_scale("HOLD 0.7S", EPD_W - 16, T_BIG);
        y = cline(y, sc, "HOLD 0.7S");
        y = cline(y, T_MID, "TO RECORD");
        if (m->line[0]) {
            int s2 = epd_fit_scale(m->line, EPD_W - 20, T_SMALL);
            y = cline(y, s2, m->line);
        }
        if (m->hint[0]) {
            cline(y, T_MID, m->hint);
        }
        break;
    }
    }

    epd_present();
}

/* 关键画面：一直等到屏真的刷完再返回 */
void epd_ui_render_blocking(const screen_model_t *m)
{
    epd_ui_render(m);
    epd_present_blocking();
}

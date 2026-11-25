/* 1.54" 电子墨水屏 200x200 驱动
 *
 * 时序和波形表（LUT）照抄微雪官方示例
 *   02_Example/ESP-IDF/V2/08_Audio_Test/components/epaper_driver_bsp/epaper_driver_bsp.cpp
 * 引脚来自同仓库 main/user_config.h。
 *
 * 相比官方 C++ 版这里做了三处改造：
 *   1. 刷新放到独立任务里，主循环（录音 20ms 一帧）不再被 BUSY 等待卡住；
 *   2. 每次刷新前比对帧缓冲，内容没变就不刷，省电也省屏的寿命；
 *   3. 每 N 次局部刷新插一次全刷，避免残影越积越重。
 */
#include "epd_panel.h"

#include <ctype.h>
#include <string.h>

#include "board.h"
#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "ui_font.h"

static const char *TAG = "epd";

#define EPD_HOST SPI2_HOST
#define EPD_SPI_HZ (10 * 1000 * 1000) /* SSD1681 手册标称最高 10MHz */
#define EPD_ROW_BYTES ((EPD_W + 7) / 8)
#define EPD_FB_BYTES (EPD_ROW_BYTES * EPD_H)

/* 两次刷新之间的最小间隔（局部刷新本身要 300~500ms） */
#define EPD_MIN_INTERVAL_MS 700
/* 每 24 次局部刷新插一次全刷，清残影 */
#define EPD_FULL_EVERY 24

/* ------------------------------------------------------------------ *
 * 波形表（官方 LUT，勿改）
 * ------------------------------------------------------------------ */
static const uint8_t s_lut_full[159] = {
    0x80, 0x48, 0x40, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x40, 0x48, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x80, 0x48, 0x40, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x40, 0x48, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x0A, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x08, 0x01, 0x00, 0x08, 0x01,
    0x00, 0x02, 0x0A, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x22, 0x22, 0x22, 0x22, 0x22, 0x22, 0x00, 0x00, 0x00, 0x22, 0x17, 0x41,
    0x00, 0x32, 0x20,
};

static const uint8_t s_lut_partial[159] = {
    0x00, 0x40, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x80, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x40, 0x40, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x0F, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x01, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x22, 0x22, 0x22, 0x22, 0x22, 0x22, 0x00, 0x00, 0x00, 0x02, 0x17, 0x41,
    0xB0, 0x32, 0x28,
};

/* ------------------------------------------------------------------ *
 * 状态
 * ------------------------------------------------------------------ */
static spi_device_handle_t s_spi;
static uint8_t *s_back;    /* 界面在画的后缓冲 */
static uint8_t *s_pending; /* 交给刷新任务的缓冲 */
static uint8_t *s_shown;   /* 屏上当前内容，用来判断"有没有变" */
static SemaphoreHandle_t s_mux;
static TaskHandle_t s_task;
static volatile bool s_dirty;
static volatile bool s_flushing; /* 正在往屏上刷（包含等 BUSY）*/
static bool s_ok;
static volatile bool s_off; /* 面板被 epd_power_off() 断了电，画面还留在屏上 */
static int s_refresh_cnt;

static inline void dc_cmd(void)
{
    gpio_set_level(PIN_EPD_DC, 0);
}

static inline void dc_data(void)
{
    gpio_set_level(PIN_EPD_DC, 1);
}

static inline void cs_sel(void)
{
    gpio_set_level(PIN_EPD_CS, 0);
}

static inline void cs_desel(void)
{
    gpio_set_level(PIN_EPD_CS, 1);
}

static void spi_write(const uint8_t *buf, int len)
{
    spi_transaction_t t = {0};
    t.length = 8 * len;
    t.tx_buffer = buf;
    cs_sel();
    esp_err_t err = spi_device_polling_transmit(s_spi, &t);
    cs_desel();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "SPI 传输失败：%s", esp_err_to_name(err));
    }
}

static void send_cmd(uint8_t c)
{
    dc_cmd();
    spi_write(&c, 1);
}

static void send_data(uint8_t d)
{
    dc_data();
    spi_write(&d, 1);
}

static void send_data_buf(const uint8_t *buf, int len)
{
    dc_data();
    spi_write(buf, len);
}

static void wait_idle(void)
{
    /* BUSY 脚：高=忙，低=空闲。
     * 先无条件等 20ms：有些时候命令刚发出去 BUSY 还没拉起来，
     * 这时候立刻读会读到低电平，以为已经空闲，结果刷新被中途打断 → 满屏噪点。 */
    vTaskDelay(pdMS_TO_TICKS(20));
    int spins = 0;
    while (gpio_get_level(PIN_EPD_BUSY) == 1) {
        vTaskDelay(pdMS_TO_TICKS(5));
        if (++spins > 400) { /* 2 秒还没好，认为屏挂了 */
            ESP_LOGW(TAG, "BUSY 一直不落，放弃这次刷新");
            break;
        }
    }
}

static void set_lut(const uint8_t *lut)
{
    send_cmd(0x32);
    send_data_buf(lut, 153);
    wait_idle();

    send_cmd(0x3f);
    send_data(lut[153]);

    send_cmd(0x03);
    send_data(lut[154]);

    send_cmd(0x04);
    send_data(lut[155]);
    send_data(lut[156]);
    send_data(lut[157]);

    send_cmd(0x2c);
    send_data(lut[158]);
}

/* 窗口和光标的取值原样照抄官方驱动：X 从 0 递增，Y 从 H-1 递减。
 * （EPD_SetWindows(0, Width-1, Height-1, 0) + EPD_SetCursor(0, Height-1)）
 * 自己"顺手改成递增"会让扫描方向和数据顺序对不上，画面直接错乱。 */
static void set_window(void)
{
    send_cmd(0x44); /* RAM X 起止，单位是字节（8 像素） */
    send_data(0);
    send_data((EPD_W - 1) >> 3);

    send_cmd(0x45); /* RAM Y 起止：从 H-1 到 0 */
    send_data((EPD_H - 1) & 0xFF);
    send_data((EPD_H - 1) >> 8);
    send_data(0);
    send_data(0);
}

static void set_cursor(void)
{
    send_cmd(0x4E);
    send_data(0);
    send_cmd(0x4F);
    send_data((EPD_H - 1) & 0xFF);
    send_data((EPD_H - 1) >> 8);
}

static void hw_reset(void)
{
    gpio_set_level(PIN_EPD_RST, 1);
    vTaskDelay(pdMS_TO_TICKS(50));
    gpio_set_level(PIN_EPD_RST, 0);
    vTaskDelay(pdMS_TO_TICKS(20));
    gpio_set_level(PIN_EPD_RST, 1);
    vTaskDelay(pdMS_TO_TICKS(50));
}

/* 下屏：full=true 走全刷（慢、闪一下、清残影），false 走局部刷（快） */
static void push_frame(const uint8_t *fb, bool full)
{
    send_cmd(0x24);
    send_data_buf(fb, EPD_FB_BYTES);
    if (full) {
        /* 全刷要同时给"旧图" RAM，否则会留下上一帧的鬼影 */
        send_cmd(0x26);
        send_data_buf(fb, EPD_FB_BYTES);
        send_cmd(0x22);
        send_data(0xc7);
    } else {
        send_cmd(0x22);
        send_data(0xcf);
    }
    send_cmd(0x20);
    wait_idle();
}

static void epd_task(void *arg)
{
    (void)arg;
    uint32_t last = 0;

    for (;;) {
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        if (!s_ok) {
            continue;
        }

        /* 限流：刷新本身要几百毫秒，别把屏累死也别把 CPU 占满 */
        uint32_t now = (uint32_t)(xTaskGetTickCount() * portTICK_PERIOD_MS);
        if (last && (now - last) < EPD_MIN_INTERVAL_MS) {
            vTaskDelay(pdMS_TO_TICKS(EPD_MIN_INTERVAL_MS - (now - last)));
        }

        bool full = (++s_refresh_cnt % EPD_FULL_EVERY) == 0;

        xSemaphoreTake(s_mux, portMAX_DELAY);
        memcpy(s_pending, s_back, EPD_FB_BYTES);
        s_dirty = false;
        s_flushing = true;
        xSemaphoreGive(s_mux);

        push_frame(s_pending, full);
        memcpy(s_shown, s_pending, EPD_FB_BYTES);
        s_flushing = false;
        last = (uint32_t)(xTaskGetTickCount() * portTICK_PERIOD_MS);
        ESP_LOGD(TAG, "刷新完成（%s）", full ? "全刷" : "局部");
    }
}

/* ------------------------------------------------------------------ *
 * 绘图原语（1bpp）
 * ------------------------------------------------------------------ */
static inline void px(int x, int y, uint8_t c)
{
    if ((unsigned)x >= EPD_W || (unsigned)y >= EPD_H) {
        return;
    }
    size_t idx = (size_t)y * EPD_ROW_BYTES + (x >> 3);
    uint8_t bit = (uint8_t)(7 - (x & 7));
    if (c) {
        s_back[idx] |= (uint8_t)(1u << bit);
    } else {
        s_back[idx] &= (uint8_t)~(1u << bit);
    }
}

void epd_clear(uint8_t c)
{
    if (!s_back) {
        return;
    }
    memset(s_back, c ? 0xFF : 0x00, EPD_FB_BYTES);
}

void epd_fill(int x, int y, int w, int h, uint8_t c)
{
    if (!s_back) {
        return;
    }
    int x0 = x < 0 ? 0 : x;
    int y0 = y < 0 ? 0 : y;
    int x1 = x + w;
    int y1 = y + h;
    if (x1 > EPD_W) {
        x1 = EPD_W;
    }
    if (y1 > EPD_H) {
        y1 = EPD_H;
    }
    for (int j = y0; j < y1; j++) {
        for (int i = x0; i < x1; i++) {
            px(i, j, c);
        }
    }
}

void epd_frame(int x, int y, int w, int h, uint8_t c, int t)
{
    epd_fill(x, y, w, t, c);
    epd_fill(x, y + h - t, w, t, c);
    epd_fill(x, y, t, h, c);
    epd_fill(x + w - t, y, t, h, c);
}

void epd_hline(int x, int y, int w, uint8_t c)
{
    epd_fill(x, y, w, 1, c);
}

void epd_dot(int cx, int cy, int r, uint8_t c)
{
    for (int dy = -r; dy <= r; dy++) {
        for (int dx = -r; dx <= r; dx++) {
            if (dx * dx + dy * dy <= r * r) {
                px(cx + dx, cy + dy, c);
            }
        }
    }
}

static int glyph_index(char c)
{
    c = (char)toupper((unsigned char)c);
    const char *p = strchr((const char *)ui_font_chars, c);
    return p ? (int)(p - (const char *)ui_font_chars) : -1;
}

int epd_text_w(const char *s, int scale)
{
    int n = 0;
    while (s && *s++) {
        n++;
    }
    return n > 0 ? n * UI_FONT_ADV * scale - scale : 0;
}

int epd_fit_scale(const char *s, int max_w, int max_scale)
{
    for (int sc = max_scale; sc > 1; sc--) {
        if (epd_text_w(s, sc) <= max_w) {
            return sc;
        }
    }
    return 1;
}

void epd_text(int x, int y, int scale, uint8_t fg, int32_t bg, const char *s)
{
    if (!s_back || !s) {
        return;
    }
    if (bg >= 0) {
        epd_fill(x - scale, y - scale, epd_text_w(s, scale) + 3 * scale, UI_FONT_H * scale + 2 * scale,
                 (uint8_t)bg);
    }
    for (; *s; s++) {
        int gi = glyph_index(*s);
        if (gi >= 0) {
            const uint8_t *g = &ui_font_data[gi * UI_FONT_W];
            for (int col = 0; col < UI_FONT_W; col++) {
                for (int row = 0; row < UI_FONT_H; row++) {
                    if (g[col] & (1u << row)) {
                        epd_fill(x + col * scale, y + row * scale, scale, scale, fg);
                    }
                }
            }
        }
        x += UI_FONT_ADV * scale;
    }
}

void epd_text_center(int y, int scale, uint8_t fg, int32_t bg, const char *s)
{
    epd_text((EPD_W - epd_text_w(s, scale)) / 2, y, scale, fg, bg, s);
}

void epd_text_right(int x_right, int y, int scale, uint8_t fg, int32_t bg, const char *s)
{
    epd_text(x_right - epd_text_w(s, scale), y, scale, fg, bg, s);
}

/* ------------------------------------------------------------------ *
 * 提交 / 初始化
 * ------------------------------------------------------------------ */
void epd_present(void)
{
    if (!s_ok || s_off) {
        /* 断电期间只更新后缓冲，不下屏：等页面要上电时再一次性全刷 */
        return;
    }
    xSemaphoreTake(s_mux, portMAX_DELAY);
    if (memcmp(s_back, s_shown, EPD_FB_BYTES) == 0) {
        /* 画面没变，不用刷。墨水屏最忌讳无意义刷新 */
        xSemaphoreGive(s_mux);
        return;
    }
    s_dirty = true;
    xSemaphoreGive(s_mux);
    if (s_task) {
        xTaskNotifyGive(s_task);
    }
}

void epd_present_blocking(void)
{
    epd_present();
    /* 一直等到后台任务真的把这一帧刷到屏上（s_flushing 覆盖 push_frame 全程），
     * 最多等 6 秒。关机前必须用它，否则断电会把全屏刷新截断。 */
    for (int i = 0; i < 300 && (s_dirty || s_flushing); i++) {
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

bool epd_ready(void)
{
    return s_ok;
}

void epd_power_off(void)
{
    if (!s_ok || s_off) {
        return;
    }
    if (PIN_EPD_PWR != PIN_NC) {
        /* 等在刷的那一帧真的落屏再断电，否则会把全刷截断，留下半屏 */
        for (int i = 0; i < 300 && (s_dirty || s_flushing); i++) {
            vTaskDelay(pdMS_TO_TICKS(20));
        }
        /* 墨水屏断电后画面保留，所以直接关最省电 */
        send_cmd(0x10); /* deep sleep mode */
        send_data(0x01);
        vTaskDelay(pdMS_TO_TICKS(50));
        board_epd_power(false);
        s_off = true;
        s_dirty = false;
        ESP_LOGI(TAG, "墨水屏已断电（画面保留）");
    }
}

/* 调试用的自检：依次刷 全白 / 全黑 / 竖条纹 / 横条纹，
 * 每屏停 2.5 秒并在串口打印"现在应该是 xxx"。
 * 看不到屏幕的时候，靠这个一眼就能分清是"数据不对"还是"驱动没起来"。
 * 调通之后把 EPD_SELFTEST 改成 0。 */
#ifndef EPD_SELFTEST
#define EPD_SELFTEST 0 /* 排线/驱动有疑问时改成 1，开机刷四个测试图案 */
#endif

#if EPD_SELFTEST
static void push_sync(bool full)
{
    memcpy(s_pending, s_back, EPD_FB_BYTES);
    push_frame(s_pending, full);
    memcpy(s_shown, s_pending, EPD_FB_BYTES);
}

void epd_selftest(void)
{
    if (!s_ok) {
        return;
    }
    if (s_task) {
        vTaskSuspend(s_task); /* 自检期间别让后台任务插进来 */
    }
    ESP_LOGW(TAG, "=== 墨水屏自检开始，请看屏幕 ===");

    for (int i = 0; i < 4; i++) {
        switch (i) {
        case 0:
            epd_clear(EPD_WHITE);
            ESP_LOGW(TAG, "[1/4] 屏幕上应该是：全白");
            break;
        case 1:
            epd_clear(EPD_BLACK);
            ESP_LOGW(TAG, "[2/4] 屏幕上应该是：全黑");
            break;
        case 2:
            epd_clear(EPD_WHITE);
            for (int x = 0; x < EPD_W; x += 16) {
                epd_fill(x, 0, 8, EPD_H, EPD_BLACK);
            }
            ESP_LOGW(TAG, "[3/4] 屏幕上应该是：竖条纹（每 16 像素一条粗竖线）");
            break;
        default:
            epd_clear(EPD_WHITE);
            for (int y = 0; y < EPD_H; y += 16) {
                epd_fill(0, y, EPD_W, 8, EPD_BLACK);
            }
            ESP_LOGW(TAG, "[4/4] 屏幕上应该是：横条纹（每 16 像素一条粗横线）");
            break;
        }
        push_sync(true);
        vTaskDelay(pdMS_TO_TICKS(2500));
    }

    epd_clear(EPD_WHITE);
    push_sync(true);
    ESP_LOGW(TAG, "=== 自检结束 ===");
    if (s_task) {
        vTaskResume(s_task);
    }
}
#else
void epd_selftest(void)
{
}
#endif

/* SSD1681 上电后的完整初始化：复位 → 寄存器 → 全刷一帧打底 → 切局部刷新。
 * 开机时传全白（清残影），从暂停唤醒时传当前界面，这样唤醒只闪一次全刷。
 * 面板断电后寄存器全丢，必须重新走完这一套才能继续刷新。 */
static void panel_bringup(const uint8_t *fb)
{
    hw_reset();
    wait_idle();
    send_cmd(0x12); /* SW RESET */
    wait_idle();

    send_cmd(0x01); /* driver output control */
    send_data(0xC7);
    send_data(0x00);
    send_data(0x01);

    send_cmd(0x11); /* data entry mode */
    send_data(0x01);

    set_window();

    send_cmd(0x3C); /* border waveform */
    send_data(0x01);

    send_cmd(0x18); /* temperature sensor */
    send_data(0x80);

    send_cmd(0x22);
    send_data(0xB1);
    send_cmd(0x20);
    set_cursor();
    wait_idle();

    set_lut(s_lut_full);
    push_frame(fb, true);

    /* ---- 切到局部刷新模式 ---- */
    hw_reset();
    wait_idle();
    set_lut(s_lut_partial);

    send_cmd(0x37); /* 边界/局部刷新窗口（官方给的一组魔数） */
    send_data(0x00);
    send_data(0x00);
    send_data(0x00);
    send_data(0x00);
    send_data(0x00);
    send_data(0x40);
    send_data(0x00);
    send_data(0x00);
    send_data(0x00);
    send_data(0x00);

    send_cmd(0x3C);
    send_data(0x80);

    send_cmd(0x22);
    send_data(0xc0);
    send_cmd(0x20);
    wait_idle();

    set_window();
    set_cursor();
}

esp_err_t epd_init(void)
{
    if (s_ok) {
        return ESP_OK;
    }
#if PIN_EPD_PWR == PIN_NC
    return ESP_ERR_NOT_SUPPORTED;
#else
    board_epd_power(true);
    vTaskDelay(pdMS_TO_TICKS(10));

    gpio_config_t out = {
        .pin_bit_mask = (1ULL << PIN_EPD_DC) | (1ULL << PIN_EPD_CS) | (1ULL << PIN_EPD_RST),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&out));
    gpio_config_t in = {
        .pin_bit_mask = 1ULL << PIN_EPD_BUSY,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&in));
    gpio_set_level(PIN_EPD_RST, 1);

    spi_bus_config_t bus = {
        .sclk_io_num = PIN_EPD_SCLK,
        .mosi_io_num = PIN_EPD_MOSI,
        .miso_io_num = -1,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = EPD_FB_BYTES,
    };
    esp_err_t err = spi_bus_initialize(EPD_HOST, &bus, SPI_DMA_CH_AUTO);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        ESP_LOGE(TAG, "SPI 总线：%s", esp_err_to_name(err));
        return err;
    }
    spi_device_interface_config_t dev = {
        .spics_io_num = -1, /* CS 自己用 GPIO 控，官方驱动也是这么干的 */
        .clock_speed_hz = EPD_SPI_HZ,
        .mode = 0,
        .queue_size = 4,
    };
    err = spi_bus_add_device(EPD_HOST, &dev, &s_spi);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "SPI 设备：%s", esp_err_to_name(err));
        return err;
    }

    /* 必须放内部 RAM：开了 PSRAM 之后普通的 malloc 会落到 PSRAM 上，
     * 而 PSRAM 直接当 SPI DMA 的源在 S3 上不稳，传出去的数据是脏的
     * （表现就是满屏随机黑点）。三个缓冲一共才 15KB，内部 RAM 完全装得下。 */
    s_back = heap_caps_malloc(EPD_FB_BYTES, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    s_pending = heap_caps_malloc(EPD_FB_BYTES, MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA);
    s_shown = heap_caps_malloc(EPD_FB_BYTES, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    s_mux = xSemaphoreCreateMutex();
    if (!s_back || !s_pending || !s_shown || !s_mux) {
        ESP_LOGE(TAG, "显存分配失败");
        return ESP_ERR_NO_MEM;
    }
    memset(s_back, 0xFF, EPD_FB_BYTES);
    memset(s_shown, 0x00, EPD_FB_BYTES); /* 故意和 back 不同，保证首帧一定刷 */

    panel_bringup(s_back);
    memcpy(s_shown, s_back, EPD_FB_BYTES);

    s_ok = true;
    s_refresh_cnt = 0;

    /* BUSY 引脚诊断：恒低说明引脚没接对或者 BUSY 信号没过来，
     * 那种情况下刷新不等 BUSY 就会被打断，画面必然是噪点 */
    ESP_LOGI(TAG, "BUSY(GPIO%d) 现在电平=%d（刷新前应为 0）", PIN_EPD_BUSY,
             gpio_get_level(PIN_EPD_BUSY));

    /* 栈给 6KB：SPI 传输 + 日志格式化调用链不浅，3KB 太悬 */
    BaseType_t r = xTaskCreatePinnedToCore(epd_task, "epd", 6 * 1024, NULL, 5, &s_task, 1);
    if (r != pdPASS) {
        ESP_LOGW(TAG, "刷新任务没起来，退回同步刷新");
        s_task = NULL;
    }

    ESP_LOGI(TAG, "墨水屏 %dx%d 就绪（SPI %dMHz，局部刷新）", EPD_W, EPD_H, EPD_SPI_HZ / 1000000);

    epd_selftest();
    return ESP_OK;
#endif
}

esp_err_t epd_resume(void)
{
    if (!s_ok || !s_off) {
        return ESP_OK; /* 没断过电就什么都不用做 */
    }
#if PIN_EPD_PWR == PIN_NC
    return ESP_ERR_NOT_SUPPORTED;
#else
    /* 断电期间 UI 可能已经画了新一帧，先把它拷出来再上电 */
    xSemaphoreTake(s_mux, portMAX_DELAY);
    memcpy(s_pending, s_back, EPD_FB_BYTES);
    xSemaphoreGive(s_mux);

    board_epd_power(true);
    vTaskDelay(pdMS_TO_TICKS(10));
    panel_bringup(s_pending);

    xSemaphoreTake(s_mux, portMAX_DELAY);
    memcpy(s_shown, s_pending, EPD_FB_BYTES);
    s_off = false;
    s_dirty = false;
    s_flushing = false;
    xSemaphoreGive(s_mux);
    s_refresh_cnt = 0;

    ESP_LOGI(TAG, "墨水屏重新上电（已按当前画面全刷一次）");
    return ESP_OK;
#endif
}

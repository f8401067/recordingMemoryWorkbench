#include "display.h"

#include <ctype.h>
#include <stdio.h>
#include <string.h>

#include "app_config.h"
#include "board.h"
#include "esp_log.h"
#include "lcd_panel.h"
#include "ui_font.h"

static const char *TAG = "disp";

#if CONFIG_RECALL_DISPLAY_ST7789_320

/* ------------------------------------------------------------------ *
 * ST7789V2 320x240（正点原子 BOX3 这块屏）
 * 排版在 lcd_ui.c，这里只管把面板拉起来
 * ------------------------------------------------------------------ */
static bool s_ok;

esp_err_t display_init(void)
{
    esp_err_t err = lcd_init();
    if (err != ESP_OK) {
        /* 屏没起来不影响录音，别让它拖死开机 */
        ESP_LOGW(TAG, "ST7789 没起来（%s），按无屏跑", esp_err_to_name(err));
        return ESP_OK;
    }
    s_ok = true;
    return ESP_OK;
}

bool display_available(void)
{
    return s_ok;
}

void display_render(const screen_model_t *m)
{
    if (!s_ok || !m) {
        return;
    }
    lcd_ui_render(m);
}

void display_power(bool on)
{
    board_backlight(on);
}

#else /* 单色 128x64 通道（SSD1306 / ST7735 / 无屏） */

#define CV_W 128
#define CV_H 64
#define CV_PAGES (CV_H / 8)

static uint8_t s_cv[CV_PAGES][CV_W];
static bool s_ok;

/* ------------------------------------------------------------------ *
 * 画布
 * ------------------------------------------------------------------ */
static void cv_clear(void)
{
    memset(s_cv, 0, sizeof(s_cv));
}

static void cv_px(int x, int y, bool on)
{
    if (x < 0 || x >= CV_W || y < 0 || y >= CV_H) {
        return;
    }
    if (on) {
        s_cv[y >> 3][x] |= (uint8_t)(1u << (y & 7));
    } else {
        s_cv[y >> 3][x] &= (uint8_t) ~(1u << (y & 7));
    }
}

static void cv_hline(int x0, int x1, int y, bool on)
{
    for (int x = x0; x <= x1; x++) {
        cv_px(x, y, on);
    }
}

static void cv_rect(int x, int y, int w, int h, bool on)
{
    for (int j = 0; j < h; j++) {
        cv_hline(x, x + w - 1, y + j, on);
    }
}

static void cv_circle(int cx, int cy, int r, bool on)
{
    for (int y = -r; y <= r; y++) {
        for (int x = -r; x <= r; x++) {
            if (x * x + y * y <= r * r) {
                cv_px(cx + x, cy + y, on);
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

static int text_width(const char *s, int scale)
{
    int n = 0;
    for (; *s; s++) {
        n++;
    }
    return n > 0 ? n * UI_FONT_ADV * scale - scale : 0;
}

static void cv_text(int x, int y, int scale, const char *s)
{
    for (; *s; s++) {
        int gi = glyph_index(*s);
        if (gi >= 0) {
            const uint8_t *g = &ui_font_data[gi * UI_FONT_W];
            for (int col = 0; col < UI_FONT_W; col++) {
                for (int row = 0; row < UI_FONT_H; row++) {
                    if (g[col] & (1u << row)) {
                        if (scale == 1) {
                            cv_px(x + col, y + row, true);
                        } else {
                            cv_rect(x + col * scale, y + row * scale, scale, scale, true);
                        }
                    }
                }
            }
        }
        x += UI_FONT_ADV * scale;
    }
}

static void cv_text_centered(int y, int scale, const char *s)
{
    int w = text_width(s, scale);
    cv_text((CV_W - w) / 2, y, scale, s);
}

static void cv_text_right(int x_right, int y, int scale, const char *s)
{
    cv_text(x_right - text_width(s, scale), y, scale, s);
}

/* ------------------------------------------------------------------ *
 * 驱动选择
 * ------------------------------------------------------------------ */
#if CONFIG_RECALL_DISPLAY_SSD1306 || CONFIG_RECALL_DISPLAY_AUTO

#include "driver/i2c_master.h"

static i2c_master_bus_handle_t s_bus;
static i2c_master_dev_handle_t s_dev;
static uint8_t s_addr = 0x3C;

static esp_err_t ssd_cmd(const uint8_t *cmds, size_t n)
{
    uint8_t buf[32];
    if (n + 1 > sizeof(buf)) {
        return ESP_ERR_INVALID_SIZE;
    }
    buf[0] = 0x00; /* control: 后续全是命令 */
    memcpy(buf + 1, cmds, n);
    return i2c_master_transmit(s_dev, buf, n + 1, 100);
}

#define SSD_CMD(...)                                     \
    do {                                                 \
        const uint8_t c[] = {__VA_ARGS__};               \
        ssd_cmd(c, sizeof(c));                           \
    } while (0)

esp_err_t display_init(void)
{
    /* 总线由 board.c 统一创建，这里只是借用（重复初始化 I2C 端口会 assert） */
    s_bus = board_i2c_bus();
    if (!s_bus) {
        ESP_LOGW(TAG, "没有可用的 I2C 总线，按无屏跑");
        return ESP_OK;
    }
    esp_err_t err = ESP_OK;

#if CONFIG_RECALL_DISPLAY_AUTO
    /* 自动模式：先看 I2C 上有没有 OLED（0x3C 最常见，0x3D 是改了地址的模块）。
     * 都没有就当无屏跑，s_ok 保持 false，界面函数自动变成空操作。 */
    if (i2c_master_probe(s_bus, 0x3C, 200) == ESP_OK) {
        s_addr = 0x3C;
    } else if (i2c_master_probe(s_bus, 0x3D, 200) == ESP_OK) {
        s_addr = 0x3D;
        ESP_LOGW(TAG, "OLED 用的是 0x3D 地址");
    } else {
        ESP_LOGW(TAG, "I2C 上没找到 OLED（探了 0x3C/0x3D），按无屏跑：LED + 蜂鸣器");
        return ESP_OK;
    }
#endif

    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = s_addr,
        .scl_speed_hz = 400000,
    };
    err = i2c_master_bus_add_device(s_bus, &dev_cfg, &s_dev);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "i2c dev: %s", esp_err_to_name(err));
        return err;
    }

    SSD_CMD(0xAE);        /* display off */
    SSD_CMD(0xD5, 0x80);  /* clock div */
    SSD_CMD(0xA8, 0x3F);  /* multiplex 64 */
    SSD_CMD(0xD3, 0x00);  /* display offset */
    SSD_CMD(0x40);        /* start line 0 */
    SSD_CMD(0x8D, 0x14);  /* charge pump on */
    SSD_CMD(0x20, 0x00);  /* horizontal addressing */
    SSD_CMD(0xA1);        /* seg remap */
    SSD_CMD(0xC8);        /* com scan dec */
    SSD_CMD(0xDA, 0x12);
    SSD_CMD(0x81, 0xCF);  /* contrast */
    SSD_CMD(0xD9, 0xF1);
    SSD_CMD(0xDB, 0x40);
    SSD_CMD(0xA4);        /* resume from RAM */
    SSD_CMD(0xA6);        /* normal (非反色) */
    SSD_CMD(0x2E);        /* stop scroll */
    SSD_CMD(0xAF);        /* display on */

    s_ok = true;
    ESP_LOGI(TAG, "ssd1306 ready on I2C (%d/%d, addr 0x%02X)", PIN_I2C_SDA, PIN_I2C_SCL, s_addr);
    return ESP_OK;
}

static void flush(void)
{
    if (!s_ok) {
        return;
    }
    uint8_t buf[1 + CV_W];
    for (int page = 0; page < CV_PAGES; page++) {
        SSD_CMD((uint8_t)(0xB0 | page));
        SSD_CMD(0x00);
        SSD_CMD(0x10);
        buf[0] = 0x40; /* control: 后续是显存数据 */
        memcpy(buf + 1, s_cv[page], CV_W);
        i2c_master_transmit(s_dev, buf, sizeof(buf), 100);
    }
}

void display_power(bool on)
{
    if (!s_ok) {
        return;
    }
    SSD_CMD(on ? 0xAF : 0xAE);
}

#elif CONFIG_RECALL_DISPLAY_ST7735

#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

/* 常见 160x80 ST7735S 模块（TTGO T-Display / 淘宝同类）。
 * 颜色不对就调 INVON/NORON，画面偏移就调 X_OFF/Y_OFF。 */
#define TFT_W 160
#define TFT_H 80
#define X_OFF ((TFT_W - CV_W) / 2)
#define Y_OFF ((TFT_H - CV_H) / 2)

static spi_device_handle_t s_spi;

static void tft_cmd(uint8_t cmd)
{
    gpio_set_level(PIN_TFT_DC, 0);
    spi_transaction_t t = {.length = 8, .tx_buffer = &cmd};
    spi_device_polling_transmit(s_spi, &t);
}

static void tft_data(const uint8_t *data, size_t len)
{
    if (!len) {
        return;
    }
    gpio_set_level(PIN_TFT_DC, 1);
    spi_transaction_t t = {.length = len * 8, .tx_buffer = data};
    spi_device_polling_transmit(s_spi, &t);
}

static void tft_init_seq(void)
{
    tft_cmd(0x01); /* SWRESET */
    vTaskDelay(pdMS_TO_TICKS(120));
    tft_cmd(0x11); /* SLPOUT */
    vTaskDelay(pdMS_TO_TICKS(120));

    uint8_t fr[] = {0x01, 0x2C, 0x2D};
    tft_cmd(0xB1);
    tft_data(fr, sizeof(fr));

    uint8_t pw[] = {0xA2, 0x02, 0x84};
    tft_cmd(0xC0);
    tft_data(pw, sizeof(pw));

    uint8_t vc[] = {0x0E, 0x0E, 0x0A, 0x00};
    tft_cmd(0xC5);
    tft_data(vc, sizeof(vc));

    uint8_t cm[] = {0x05}; /* 16bit/pixel */
    tft_cmd(0x3A);
    tft_data(cm, sizeof(cm));

    uint8_t mad[] = {0x00};
    tft_cmd(0x36);
    tft_data(mad, sizeof(mad));

    tft_cmd(0x21); /* INVON：多数模块需要反色，画面发白就删掉这行 */
    tft_cmd(0x13); /* NORON */
    vTaskDelay(pdMS_TO_TICKS(10));
    tft_cmd(0x29); /* DISPON */
    vTaskDelay(pdMS_TO_TICKS(20));
}

esp_err_t display_init(void)
{
    gpio_config_t io = {
        .pin_bit_mask = (1ULL << PIN_TFT_DC) | (1ULL << PIN_TFT_RST),
        .mode = GPIO_MODE_OUTPUT,
    };
    gpio_config(&io);
    gpio_set_level(PIN_TFT_RST, 0);
    vTaskDelay(pdMS_TO_TICKS(20));
    gpio_set_level(PIN_TFT_RST, 1);
    vTaskDelay(pdMS_TO_TICKS(120));

    spi_bus_config_t bus = {
        .mosi_io_num = PIN_TFT_MOSI,
        .miso_io_num = -1,
        .sclk_io_num = PIN_TFT_SCLK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = TFT_W * 2 * 8,
    };
    esp_err_t err = spi_bus_initialize(SPI2_HOST, &bus, SPI_DMA_CH_AUTO);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "spi bus: %s", esp_err_to_name(err));
        return err;
    }
    spi_device_interface_config_t dev = {
        .clock_speed_hz = 26 * 1000 * 1000,
        .mode = 0,
        .spics_io_num = PIN_TFT_CS,
        .queue_size = 1,
    };
    err = spi_bus_add_device(SPI2_HOST, &dev, &s_spi);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "spi dev: %s", esp_err_to_name(err));
        return err;
    }
    tft_init_seq();
    s_ok = true;
    ESP_LOGI(TAG, "st7735 ready (160x80)");
    return ESP_OK;
}

static void flush(void)
{
    if (!s_ok) {
        return;
    }
    uint8_t win[4];
    win[0] = 0;
    win[1] = X_OFF;
    win[2] = 0;
    win[3] = X_OFF + CV_W - 1;
    tft_cmd(0x2A);
    tft_data(win, 4);
    win[0] = 0;
    win[1] = Y_OFF;
    win[2] = 0;
    win[3] = Y_OFF + CV_H - 1;
    tft_cmd(0x2B);
    tft_data(win, 4);
    tft_cmd(0x2C);

    /* 逐行转 RGB565 推出去，避免 16KB 的整屏缓冲 */
    static uint8_t line[CV_W * 2];
    gpio_set_level(PIN_TFT_DC, 1);
    for (int y = 0; y < CV_H; y++) {
        for (int x = 0; x < CV_W; x++) {
            bool on = s_cv[y >> 3][x] & (1u << (y & 7));
            uint16_t c = on ? 0xFFFF : 0x0000;
            line[x * 2] = (uint8_t)(c >> 8);
            line[x * 2 + 1] = (uint8_t)(c & 0xFF);
        }
        spi_transaction_t t = {.length = sizeof(line) * 8, .tx_buffer = line};
        spi_device_polling_transmit(s_spi, &t);
    }
}

void display_power(bool on)
{
    if (!s_ok) {
        return;
    }
    tft_cmd(on ? 0x29 : 0x28);
}

#else /* 无屏幕 */

esp_err_t display_init(void)
{
    ESP_LOGI(TAG, "无屏幕配置：状态靠 LED 与蜂鸣器反馈");
    return ESP_OK;
}

static void flush(void) {}

void display_power(bool on)
{
    (void)on;
}

#endif

bool display_available(void)
{
    return s_ok;
}

/* ------------------------------------------------------------------ *
 * 界面
 * ------------------------------------------------------------------ */
static void fmt_time(uint32_t secs, char out[8])
{
    /* 封顶 99:59：录音不会超过这个长度，也免得编译器担心 %02u 溢出 8 字节 */
    if (secs > 99 * 60 + 59) {
        secs = 99 * 60 + 59;
    }
    snprintf(out, 8, "%02u:%02u", (unsigned)(secs / 60), (unsigned)(secs % 60));
}

void display_render(const screen_model_t *m)
{
    if (!s_ok) {
        return;
    }
    cv_clear();

    /* 顶栏 */
    cv_text(1, 1, 1, m->title);
    if (m->code[0] && m->kind != SCR_PROV) {
        /* 配网页已经把编号写进热点名（RECALL-485412），右上角就不重复了 */
        cv_text_right(CV_W - 1, 1, 1, m->code);
    }
    cv_hline(0, CV_W - 1, 10, true);

    char buf[32];
    switch (m->kind) {
    case SCR_REC: {
        /* 录音红点（闪烁）+ 大号计时 */
        if (m->blink) {
            cv_circle(9, 24, 4, true);
        }
        char t[8];
        fmt_time(m->secs, t);
        cv_text_centered(15, 2, t);

        /* 电平条：20 格 */
        int cells = 20;
        int on_cells = (m->level * cells + 99) / 100;
        for (int i = 0; i < cells; i++) {
            cv_rect(4 + i * 6, 46, 4, 8, i < on_cells);
        }
        cv_text(1, 56, 1, m->hint);
        break;
    }
    case SCR_PROV: {
        /* 一行一句，照着做就行：连这个热点 → 开这个地址 */
        cv_text_centered(13, 1, "CONNECT TO");
        const char *ap = m->line[0] ? m->line : "RECALL";
        int w = text_width(ap, 1);
        int bx = (CV_W - w) / 2 - 3;
        cv_hline(bx, bx + w + 5, 24, true);
        cv_hline(bx, bx + w + 5, 34, true);
        for (int y = 24; y <= 34; y++) {
            cv_px(bx, y, true);
            cv_px(bx + w + 5, y, true);
        }
        cv_text_centered(26, 1, ap);
        cv_text_centered(44, 1, m->hint[0] ? m->hint : "192.168.4.1");
        cv_text_centered(56, 1, "2.4G ONLY");
        break;
    }

    case SCR_PAIR:
        cv_text_centered(13, 2, "PAIR");
        cv_text_centered(30, 3, m->code);
        cv_text_centered(54, 1, m->hint[0] ? m->hint : "ADD IN WEB UI");
        break;

    case SCR_UPLOAD:
        snprintf(buf, sizeof(buf), "%d/%d", m->step, m->steps);
        cv_text_centered(14, 2, m->line[0] ? m->line : "SEND");
        cv_text_centered(36, 1, buf);
        if (m->steps > 0) {
            /* 外框 + 内部进度 */
            const int bx = 14, bw = 100, by = 50, bh = 6;
            cv_hline(bx, bx + bw - 1, by, true);
            cv_hline(bx, bx + bw - 1, by + bh - 1, true);
            for (int y = by; y < by + bh; y++) {
                cv_px(bx, y, true);
                cv_px(bx + bw - 1, y, true);
            }
            int done = (int)((int64_t)(bw - 4) * m->step / m->steps);
            cv_rect(bx + 2, by + 2, done, bh - 4, true);
        }
        cv_text(1, 57, 1, m->hint);
        break;

    case SCR_DONE:
        cv_text_centered(14, 2, "SENT");
        cv_text_centered(40, 1, m->line);
        cv_text(1, 57, 1, m->hint);
        break;

    case SCR_SKIP:
        cv_text_centered(14, 2, "SKIP");
        cv_text_centered(40, 1, m->line[0] ? m->line : "NO VOICE");
        cv_text(1, 57, 1, m->hint);
        break;

    case SCR_ERR:
        cv_text_centered(14, 2, "ERR");
        cv_text_centered(40, 1, m->line);
        cv_text(1, 57, 1, m->hint);
        break;

    case SCR_READY:
    default:
        cv_text_centered(14, 2, "HOLD 0.7S");
        cv_text_centered(34, 1, "TO RECORD");
        if (m->line[0]) {
            cv_text_centered(46, 1, m->line);
        }
        if (m->hint[0]) {
            cv_text_centered(56, 1, m->hint);
        }
        break;
    }

    flush();
}

#endif /* !CONFIG_RECALL_DISPLAY_ST7789_320 */

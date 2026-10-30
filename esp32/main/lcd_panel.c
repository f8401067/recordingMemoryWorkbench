#include "lcd_panel.h"

#include <ctype.h>
#include <stdlib.h>
#include <string.h>

#include "app_config.h"
#include "board.h"
#include "driver/spi_master.h"
#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_lcd_panel_dev.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_panel_st7789.h"
#include "esp_log.h"
#include "ui_font.h"

static const char *TAG = "lcd";

#define LCD_HOST SPI2_HOST
#define LCD_BAND_LINES 24 /* 每次 DMA 推 24 行，按 240 宽约 11KB */

static esp_lcd_panel_handle_t s_panel;
static uint16_t *s_fb;      /* PSRAM 里的整屏帧缓冲 */
static uint16_t *s_band;    /* 内部 RAM 的 DMA 中转 */
static bool s_ok;

bool lcd_ready(void)
{
    return s_ok;
}

static inline void px(int x, int y, uint16_t c)
{
    if ((unsigned)x < LCD_W && (unsigned)y < LCD_H) {
        s_fb[y * LCD_W + x] = c;
    }
}

void lcd_clear(uint16_t c)
{
    if (!s_fb) {
        return;
    }
    for (int i = 0; i < LCD_W * LCD_H; i++) {
        s_fb[i] = c;
    }
}

void lcd_fill(int x, int y, int w, int h, uint16_t c)
{
    if (!s_fb) {
        return;
    }
    int x0 = x < 0 ? 0 : x;
    int y0 = y < 0 ? 0 : y;
    int x1 = x + w;
    int y1 = y + h;
    if (x1 > LCD_W) {
        x1 = LCD_W;
    }
    if (y1 > LCD_H) {
        y1 = LCD_H;
    }
    for (int j = y0; j < y1; j++) {
        uint16_t *row = &s_fb[j * LCD_W];
        for (int i = x0; i < x1; i++) {
            row[i] = c;
        }
    }
}

void lcd_frame(int x, int y, int w, int h, uint16_t c, int t)
{
    lcd_fill(x, y, w, t, c);
    lcd_fill(x, y + h - t, w, t, c);
    lcd_fill(x, y, t, h, c);
    lcd_fill(x + w - t, y, t, h, c);
}

int lcd_text_w(const char *s, int scale)
{
    int n = 0;
    while (s && *s++) {
        n++;
    }
    return n > 0 ? n * UI_FONT_ADV * scale - scale : 0;
}

int lcd_fit_scale(const char *s, int max_w, int max_scale)
{
    for (int sc = max_scale; sc > 1; sc--) {
        if (lcd_text_w(s, sc) <= max_w) {
            return sc;
        }
    }
    return 1;
}

static int glyph_index(char c)
{
    c = (char)toupper((unsigned char)c);
    const char *p = strchr((const char *)ui_font_chars, c);
    return p ? (int)(p - (const char *)ui_font_chars) : -1;
}

void lcd_text(int x, int y, int scale, uint16_t fg, int32_t bg, const char *s)
{
    if (!s_fb || !s) {
        return;
    }
    if (bg >= 0) {
        /* 先把整行底涂掉，再点字，比逐格涂底快得多 */
        lcd_fill(x, y, lcd_text_w(s, scale) + scale, UI_FONT_H * scale, (uint16_t)bg);
    }
    for (; *s; s++) {
        int gi = glyph_index(*s);
        if (gi >= 0) {
            const uint8_t *g = &ui_font_data[gi * UI_FONT_W];
            for (int col = 0; col < UI_FONT_W; col++) {
                for (int row = 0; row < UI_FONT_H; row++) {
                    if (g[col] & (1u << row)) {
                        lcd_fill(x + col * scale, y + row * scale, scale, scale, fg);
                    }
                }
            }
        }
        x += UI_FONT_ADV * scale;
    }
}

void lcd_text_center(int y, int scale, uint16_t fg, int32_t bg, const char *s)
{
    lcd_text((LCD_W - lcd_text_w(s, scale)) / 2, y, scale, fg, bg, s);
}

void lcd_text_right(int x_right, int y, int scale, uint16_t fg, int32_t bg, const char *s)
{
    lcd_text(x_right - lcd_text_w(s, scale), y, scale, fg, bg, s);
}

void lcd_dot(int cx, int cy, int r, uint16_t c)
{
    for (int dy = -r; dy <= r; dy++) {
        for (int dx = -r; dx <= r; dx++) {
            if (dx * dx + dy * dy <= r * r) {
                px(cx + dx, cy + dy, c);
            }
        }
    }
}

void lcd_present(void)
{
    if (!s_ok || !s_fb || !s_band) {
        return;
    }
    for (int y = 0; y < LCD_H; y += LCD_BAND_LINES) {
        int lines = LCD_H - y;
        if (lines > LCD_BAND_LINES) {
            lines = LCD_BAND_LINES;
        }
        memcpy(s_band, &s_fb[y * LCD_W], (size_t)LCD_W * lines * sizeof(uint16_t));
        esp_lcd_panel_draw_bitmap(s_panel, 0, y, LCD_W, y + lines, s_band);
    }
}

esp_err_t lcd_init(void)
{
    spi_bus_config_t bus = {
        .sclk_io_num = PIN_TFT_SCLK,
        .mosi_io_num = PIN_TFT_MOSI,
        .miso_io_num = PIN_TFT_MISO,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = LCD_W * LCD_BAND_LINES * sizeof(uint16_t),
    };
    esp_err_t err = spi_bus_initialize(LCD_HOST, &bus, SPI_DMA_CH_AUTO);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        ESP_LOGE(TAG, "SPI 总线：%s", esp_err_to_name(err));
        return err;
    }

    esp_lcd_panel_io_handle_t io = NULL;
    esp_lcd_panel_io_spi_config_t io_cfg = {
        .cs_gpio_num = PIN_TFT_CS,
        .dc_gpio_num = PIN_TFT_DC,
        .spi_mode = 0,
        .pclk_hz = CONFIG_RECALL_LCD_PCLK_MHZ * 1000 * 1000,
        .trans_queue_depth = 10,
        .lcd_cmd_bits = 8,
        .lcd_param_bits = 8,
    };
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)LCD_HOST, &io_cfg, &io), TAG,
                        "panel io");

    esp_lcd_panel_dev_config_t dev_cfg = {
        .reset_gpio_num = PIN_TFT_RST,
        .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
        /* 帧缓冲是 uint16_t，本机小端；让面板也按小端收，省一次翻转 */
        .data_endian = LCD_RGB_DATA_ENDIAN_LITTLE,
        .bits_per_pixel = 16,
    };
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_st7789(io, &dev_cfg, &s_panel), TAG, "new panel");

    esp_lcd_panel_reset(s_panel);
    esp_lcd_panel_init(s_panel);
    esp_lcd_panel_invert_color(s_panel, true);

    /* 方屏（BOX0 240x240）不需要旋转；宽高不等才转成横屏 */
#if (LCD_W != LCD_H) && CONFIG_RECALL_LCD_ROTATION == 0
    esp_lcd_panel_swap_xy(s_panel, true);
    esp_lcd_panel_mirror(s_panel, true, false);
#elif CONFIG_RECALL_LCD_ROTATION == 1
    esp_lcd_panel_swap_xy(s_panel, true);
    esp_lcd_panel_mirror(s_panel, true, false);
#elif CONFIG_RECALL_LCD_ROTATION == 2
    esp_lcd_panel_swap_xy(s_panel, true);
    esp_lcd_panel_mirror(s_panel, false, true);
#elif CONFIG_RECALL_LCD_ROTATION == 3
    esp_lcd_panel_swap_xy(s_panel, false);
    esp_lcd_panel_mirror(s_panel, true, true);
#else
    esp_lcd_panel_swap_xy(s_panel, false);
    esp_lcd_panel_mirror(s_panel, false, false);
#endif
    esp_lcd_panel_disp_on_off(s_panel, true);

    /* 帧缓冲放 PSRAM，DMA 中转放内部 RAM */
    s_fb = heap_caps_malloc((size_t)LCD_W * LCD_H * sizeof(uint16_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    s_band = heap_caps_malloc((size_t)LCD_W * LCD_BAND_LINES * sizeof(uint16_t), MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA);
    if (!s_fb || !s_band) {
        ESP_LOGE(TAG, "显存不够：fb=%p band=%p", s_fb, s_band);
        free(s_fb);
        free(s_band);
        s_fb = NULL;
        s_band = NULL;
        return ESP_ERR_NO_MEM;
    }

    lcd_clear(LCD_BLACK);
    lcd_present();
    board_backlight(true);

    s_ok = true;
    ESP_LOGI(TAG, "ST7789V2 %dx%d 就绪，pclk %dMHz，背光已开", LCD_W, LCD_H, CONFIG_RECALL_LCD_PCLK_MHZ);
    return ESP_OK;
}

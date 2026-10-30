#include "audio.h"

#include <string.h>

#include "app_config.h"
#include "driver/gpio.h"
#include "driver/i2s_std.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"

static const char *TAG = "audio";

#if BOARD_CODEC_ES8311

/* ------------------------------------------------------------------ *
 * ES8311 路线（BOX0）：esp_codec_dev 官方驱动
 * ------------------------------------------------------------------ */
#include "board.h"
#include "esp_codec_dev_defaults.h"
#include "esp_codec_dev.h"

/* 注意：esp_codec_dev 里 I2C 地址按 8 位算（内部再 >>1），用官方宏 */
#define ES8311_I2C_ADDR ES8311_CODEC_DEFAULT_ADDR
#define IN_GAIN_DB 30.0f /* 小智同款：远场拾音要开这么大 */

static i2s_chan_handle_t s_rx, s_tx;
static const audio_codec_data_if_t *s_data_if;
static const audio_codec_ctrl_if_t *s_ctrl_if;
static const audio_codec_gpio_if_t *s_gpio_if;
static const audio_codec_if_t *s_codec_if;
static esp_codec_dev_handle_t s_dev;
static bool s_open;

esp_err_t audio_init(void)
{
    if (s_dev) {
        return ESP_OK;
    }
    if (!board_i2c_bus()) {
        ESP_LOGE(TAG, "I2C 总线还没就绪，ES8311 配不了");
        return ESP_ERR_INVALID_STATE;
    }

    /* 双工通道：Philips 标准 I2S、16bit 立体声、不用 MCLK
     * （ES8311 从 BCLK 自己生成时钟，板载 LED 占着 MCLK 那个脚） */
    i2s_chan_config_t chan_cfg = {
        .id = I2S_NUM_0,
        .role = I2S_ROLE_MASTER,
        .dma_desc_num = 6,
        .dma_frame_num = 240,
        .auto_clear_after_cb = true,
    };
    esp_err_t err = i2s_new_channel(&chan_cfg, &s_tx, &s_rx);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "i2s_new_channel: %s", esp_err_to_name(err));
        return err;
    }

    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(APP_SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = PIN_MIC_BCLK,
            .ws = PIN_MIC_WS,
            .dout = PIN_MIC_DOUT,
            .din = PIN_MIC_DIN,
            .invert_flags = {false, false, false},
        },
    };
    err = i2s_channel_init_std_mode(s_tx, &std_cfg);
    if (err == ESP_OK) {
        err = i2s_channel_init_std_mode(s_rx, &std_cfg);
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "i2s init: %s", esp_err_to_name(err));
        return err;
    }

    audio_codec_i2s_cfg_t i2s_cfg = {
        .port = I2S_NUM_0,
        .rx_handle = s_rx,
        .tx_handle = s_tx,
    };
    s_data_if = audio_codec_new_i2s_data(&i2s_cfg);

    audio_codec_i2c_cfg_t i2c_cfg = {
        .port = 0,
        .addr = ES8311_I2C_ADDR,
        .bus_handle = board_i2c_bus(),
    };
    s_ctrl_if = audio_codec_new_i2c_ctrl(&i2c_cfg);
    s_gpio_if = audio_codec_new_gpio();

    es8311_codec_cfg_t es_cfg = {
        .ctrl_if = s_ctrl_if,
        .gpio_if = s_gpio_if,
        .codec_mode = ESP_CODEC_DEV_WORK_MODE_BOTH,
        .pa_pin = -1, /* 功放自己管（board.c 已经拉低） */
        .use_mclk = false,
        .hw_gain.pa_voltage = 5.0,
        .hw_gain.codec_dac_voltage = 3.3,
    };
    s_codec_if = es8311_codec_new(&es_cfg);

    esp_codec_dev_cfg_t dev_cfg = {
        .dev_type = ESP_CODEC_DEV_TYPE_IN,
        .codec_if = s_codec_if,
        .data_if = s_data_if,
    };
    s_dev = esp_codec_dev_new(&dev_cfg);
    if (!s_dev) {
        ESP_LOGE(TAG, "esp_codec_dev 创建失败（检查 I2C 上有没有 0x18 的 ES8311）");
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "ES8311 ready: I2S %d/%d/%d/%d, addr 0x%02X", PIN_MIC_BCLK, PIN_MIC_WS, PIN_MIC_DIN,
             PIN_MIC_DOUT, ES8311_I2C_ADDR);
    return ESP_OK;
}

esp_err_t audio_start(void)
{
    if (!s_dev || s_open) {
        return ESP_OK;
    }
    esp_codec_dev_sample_info_t fs = {
        .sample_rate = APP_SAMPLE_RATE,
        .channel = 1,
        .bits_per_sample = 16,
    };
    esp_err_t err = (esp_err_t)esp_codec_dev_open(s_dev, &fs);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "codec open: %s", esp_err_to_name(err));
        return err;
    }
    esp_codec_dev_set_in_gain(s_dev, IN_GAIN_DB);
    s_open = true;
    ESP_LOGI(TAG, "录音开：16k/16bit/mono，增益 %.0fdB", IN_GAIN_DB);
    return ESP_OK;
}

esp_err_t audio_stop(void)
{
    if (!s_dev || !s_open) {
        return ESP_OK;
    }
    esp_err_t err = (esp_err_t)esp_codec_dev_close(s_dev);
    s_open = false;
    ESP_LOGI(TAG, "录音关");
    return err;
}

int audio_read(int16_t *dst, int max_frames, uint32_t timeout_ms)
{
    (void)timeout_ms; /* esp_codec_dev_read 本身是阻塞读 */
    if (!s_dev || !s_open || max_frames <= 0) {
        return 0;
    }
    size_t bytes = (size_t)max_frames * sizeof(int16_t);
    if (esp_codec_dev_read(s_dev, dst, (int)bytes) != ESP_CODEC_DEV_OK) {
        return 0;
    }
    return max_frames;
}

void audio_mic_power(bool on)
{
    (void)on; /* ES8311 的供电由 board.c 的 CODEC_PWR 管 */
}

#else /* BOARD_CODEC_ES8311 == 0：INMP441 数字麦直采 */

static i2s_chan_handle_t s_rx;
static bool s_running;
static int s_bytes_per_sample;
static float s_dc_x1, s_dc_y1;

static int32_t s_raw[APP_FRAME_SAMPLES * 2];

void audio_mic_power(bool on)
{
#if PIN_MIC_PWR_EN != PIN_NC
    gpio_set_level(PIN_MIC_PWR_EN, on ? 1 : 0);
#else
    (void)on;
#endif
}

esp_err_t audio_init(void)
{
    if (s_rx) {
        return ESP_OK;
    }

#if PIN_MIC_PWR_EN != PIN_NC
    gpio_config_t io = {
        .pin_bit_mask = 1ULL << PIN_MIC_PWR_EN,
        .mode = GPIO_MODE_OUTPUT,
    };
    gpio_config(&io);
    gpio_set_level(PIN_MIC_PWR_EN, 0);
#endif

    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    chan_cfg.dma_desc_num = 6;
    chan_cfg.dma_frame_num = APP_FRAME_SAMPLES; /* 每块 20ms，6 块 = 120ms 缓冲 */
    chan_cfg.auto_clear = true;

    esp_err_t err = i2s_new_channel(&chan_cfg, NULL, &s_rx);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "i2s_new_channel: %s", esp_err_to_name(err));
        return err;
    }

    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(APP_SAMPLE_RATE),
        .slot_cfg = I2S_STD_MSB_SLOT_DEFAULT_CONFIG(MIC_SLOT_BITS, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = PIN_MIC_BCLK,
            .ws = PIN_MIC_WS,
            .dout = I2S_GPIO_UNUSED,
            .din = PIN_MIC_DIN,
            .invert_flags = {false, false, false},
        },
    };
    /* INMP441 只往左声道吐数据 */
    std_cfg.slot_cfg.slot_mask = I2S_STD_SLOT_LEFT;

    err = i2s_channel_init_std_mode(s_rx, &std_cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "i2s_channel_init_std_mode: %s", esp_err_to_name(err));
        return err;
    }

    s_bytes_per_sample = MIC_SLOT_BITS / 8;
    ESP_LOGI(TAG, "i2s ready: %d Hz, %d bit slot, mono", APP_SAMPLE_RATE, MIC_SLOT_BITS);
    return ESP_OK;
}

esp_err_t audio_start(void)
{
    if (!s_rx || s_running) {
        return ESP_OK;
    }
    audio_mic_power(true);
    esp_err_t err = i2s_channel_enable(s_rx);
    if (err == ESP_OK) {
        s_running = true;
        s_dc_x1 = s_dc_y1 = 0.0f;
    }
    return err;
}

esp_err_t audio_stop(void)
{
    if (!s_rx || !s_running) {
        return ESP_OK;
    }
    esp_err_t err = i2s_channel_disable(s_rx);
    s_running = false;
    audio_mic_power(false);
    return err;
}

int audio_read(int16_t *dst, int max_frames, uint32_t timeout_ms)
{
    if (!s_rx || !s_running || max_frames <= 0) {
        return 0;
    }
    int cap = (int)(sizeof(s_raw) / sizeof(s_raw[0]));
    int frames = max_frames > cap ? cap : max_frames;

    size_t bytes = 0;
    esp_err_t err = i2s_channel_read(s_rx, s_raw, (size_t)frames * s_bytes_per_sample, &bytes,
                                     pdMS_TO_TICKS(timeout_ms));
    if (err != ESP_OK) {
        return 0;
    }

    int n = (int)(bytes / s_bytes_per_sample);
    for (int i = 0; i < n; i++) {
        int32_t v;
        if (s_bytes_per_sample == 4) {
            v = s_raw[i] >> MIC_SHIFT;
        } else {
            /* 16bit 槽位：数据在低半字，做符号扩展 */
            v = (int16_t)(s_raw[i] & 0xFFFF);
        }
        if (v > 32767) {
            v = 32767;
        } else if (v < -32768) {
            v = -32768;
        }
        /* 一阶高通，去掉麦克风的直流偏置 */
        float x = (float)v;
        float y = x - s_dc_x1 + MIC_DC_ALPHA * s_dc_y1;
        s_dc_x1 = x;
        s_dc_y1 = y;
        dst[i] = (int16_t)y;
    }
    return n;
}

#endif /* BOARD_CODEC_ES8311 */

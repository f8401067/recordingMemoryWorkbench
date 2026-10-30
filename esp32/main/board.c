#include "board.h"

#include <string.h>

#include "app_config.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "board";

/* 调试开关：开机把 I2C 上所有地址扫一遍。I2C 设备找不到时临时改成 1 */
#ifndef BOARD_I2C_SCAN
#define BOARD_I2C_SCAN 0
#endif

static i2c_master_bus_handle_t s_bus;

#if BOARD_HAS_EXIO

/* AW9523B 寄存器 */
#define EXIO_REG_IN0 0x00
#define EXIO_REG_IN1 0x01
#define EXIO_REG_OUT0 0x02
#define EXIO_REG_OUT1 0x03
#define EXIO_REG_CFG0 0x04
#define EXIO_REG_CFG1 0x05
#define EXIO_REG_ID 0x10
#define EXIO_REG_CTL 0x11
#define EXIO_REG_LED0 0x12
#define EXIO_REG_LED1 0x13

#define EXIO_ADDR 0x59 /* AD1=0, AD0=1，见原理图 */

static i2c_master_dev_handle_t s_exio;
static uint16_t s_out; /* 影子寄存器，省得每次读改写 */

esp_err_t board_exio_write(uint8_t reg, uint8_t val)
{
    if (!s_exio) {
        return ESP_ERR_NOT_SUPPORTED;
    }
    uint8_t b[2] = {reg, val};
    return i2c_master_transmit(s_exio, b, sizeof(b), 100);
}

esp_err_t board_exio_read(uint8_t reg, uint8_t *val)
{
    if (!s_exio) {
        return ESP_ERR_NOT_SUPPORTED;
    }
    return i2c_master_transmit_receive(s_exio, &reg, 1, val, 1, 100);
}

static void exio_outputs(uint16_t bits, bool on)
{
    if (!s_exio) {
        return;
    }
    if (on) {
        s_out |= bits;
    } else {
        s_out &= (uint16_t)~bits;
    }
    /* 一次性写两个端口，避免中间态闪一下 */
    board_exio_write(EXIO_REG_OUT0, (uint8_t)(s_out & 0xFF));
    board_exio_write(EXIO_REG_OUT1, (uint8_t)(s_out >> 8));
}

static esp_err_t exio_init(void)
{
    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = EXIO_ADDR,
        .scl_speed_hz = 400000,
    };
    esp_err_t err = i2c_master_bus_add_device(s_bus, &dev_cfg, &s_exio);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "AW9523B 挂总线失败：%s", esp_err_to_name(err));
        return err;
    }

    uint8_t id = 0;
    if (board_exio_read(EXIO_REG_ID, &id) == ESP_OK) {
        ESP_LOGI(TAG, "AW9523B 就绪，ID=0x%02X（期望 0x23）", id);
    } else {
        ESP_LOGW(TAG, "AW9523B 读 ID 失败，背光和板载灯可能不工作");
    }

    /* P0/P1 都退出 LED 调光模式，当普通 GPIO 用 */
    board_exio_write(EXIO_REG_LED0, 0x00);
    board_exio_write(EXIO_REG_LED1, 0x00);
    /* bit4/bit5：P0/P1 推挽输出。开漏驱动不了背光的 MOS 栅极 */
    board_exio_write(EXIO_REG_CTL, 0x30);

    /* P0_0/P0_1 是按键，配成输入；其余 P0 保持复位默认（输入），不去碰
     * BAT_CHRG_EN / PA_CTRL 这些电源相关脚，免得出乱子 */
    board_exio_write(EXIO_REG_CFG0, 0x03);
    /* P1 全是输出：P1_0 背光、P1_1/2 双色灯、P1_3..P1_6 电源使能、
     * P1_7 触摸/摄像头复位。先全部拉低再单独开需要的 */
    board_exio_write(EXIO_REG_CFG1, 0x00);

    s_out = 0;
    board_exio_write(EXIO_REG_OUT0, 0x00);
    board_exio_write(EXIO_REG_OUT1, 0x00);
    vTaskDelay(pdMS_TO_TICKS(5));
    return ESP_OK;
}

void board_backlight(bool on)
{
    exio_outputs(EXIO_LCD_BL, on);
}

void board_led(bool on)
{
    exio_outputs(EXIO_LED_BLUE, on);
}

#else /* 没有 IO 扩展的板子 */

esp_err_t board_exio_write(uint8_t reg, uint8_t val)
{
    (void)reg;
    (void)val;
    return ESP_ERR_NOT_SUPPORTED;
}

esp_err_t board_exio_read(uint8_t reg, uint8_t *val)
{
    (void)reg;
    (void)val;
    return ESP_ERR_NOT_SUPPORTED;
}

void board_backlight(bool on)
{
#if BOARD_HAS_LCD_BL
    gpio_set_level(PIN_LCD_BL, on ? 1 : 0);
#else
    /* 这些板子的背光直接接 3V3 或者挂在 IO 扩展上 */
    (void)on;
#endif
}

void board_led(bool on)
{
#if PIN_LED != PIN_NC
    gpio_set_level(PIN_LED, LED_ACTIVE_LOW ? (on ? 0 : 1) : (on ? 1 : 0));
#else
    (void)on;
#endif
}

#endif

i2c_master_bus_handle_t board_i2c_bus(void)
{
    return s_bus;
}

esp_err_t board_init(void)
{
    if (s_bus) {
        return ESP_OK;
    }

#if BOARD_I2C_SCAN
    /* 穷举几个候选 SDA/SCL 组合，把真实引脚找出来（找到后写死到 app_config.h）。
     * 必须在正式总线建起来之前跑，同一个 I2C 端口同时只允许一个总线 */
    static const int8_t cands[][2] = {
        {PIN_I2C_SDA, PIN_I2C_SCL}, /* 文档给的组合 */
        {PIN_I2C_SCL, PIN_I2C_SDA}, /* 反过来接 */
        {41, 40}, {40, 41}, {8, 9}, {9, 8}, {42, 41}, {41, 42},
        {18, 17}, {17, 18}, {48, 47}, {47, 48}, {4, 5}, {5, 4},
        {1, 2}, {2, 1}, {19, 20}, {20, 19},
    };
    for (size_t i = 0; i < sizeof(cands) / sizeof(cands[0]); i++) {
        int sda = cands[i][0], scl = cands[i][1];
        i2c_master_bus_handle_t b = NULL;
        i2c_master_bus_config_t cfg = {
            .i2c_port = 0,
            .sda_io_num = sda,
            .scl_io_num = scl,
            .clk_source = I2C_CLK_SRC_DEFAULT,
            .glitch_ignore_cnt = 7,
            .flags.enable_internal_pullup = true,
        };
        if (i2c_new_master_bus(&cfg, &b) != ESP_OK) {
            ESP_LOGW(TAG, "pair SDA=%d SCL=%d: 总线建不起来", sda, scl);
            continue;
        }
        int found = 0;
        for (uint8_t a = 0x08; a < 0x78; a++) {
            if (i2c_master_probe(b, a, 20) == ESP_OK) {
                ESP_LOGW(TAG, ">> SDA=%d SCL=%d 上发现 0x%02X", sda, scl, a);
                found++;
            }
        }
        ESP_LOGW(TAG, "pair SDA=%d SCL=%d: %d 个设备", sda, scl, found);
        i2c_del_master_bus(b);
        vTaskDelay(pdMS_TO_TICKS(30));
    }
#endif

    i2c_master_bus_config_t bus_cfg = {
        .i2c_port = 0,
        .sda_io_num = PIN_I2C_SDA,
        .scl_io_num = PIN_I2C_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    esp_err_t err = i2c_new_master_bus(&bus_cfg, &s_bus);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "I2C 总线初始化失败：%s", esp_err_to_name(err));
        s_bus = NULL;
        return err;
    }
    ESP_LOGI(TAG, "I2C0 ready (SDA=%d SCL=%d)", PIN_I2C_SDA, PIN_I2C_SCL);

#if BOARD_HAS_EXIO
    exio_init();
#endif

#if BOARD_HAS_LCD_BL
    /* 背光是普通 GPIO（BOX0），开机先灭着，屏幕起来后再点亮 */
    gpio_config_t bl = {
        .pin_bit_mask = 1ULL << PIN_LCD_BL,
        .mode = GPIO_MODE_OUTPUT,
    };
    gpio_config(&bl);
    gpio_set_level(PIN_LCD_BL, 0);
#endif

#if PIN_SYS_POW != PIN_NC
    /* 电池产品的电源自锁：不拉高 SYS_POW 松手就断电；
     * CODEC_PWR 不拉高 ES8311 没电，录音就是纯静音 */
    gpio_config_t pw = {
        .pin_bit_mask = (1ULL << PIN_SYS_POW) | (1ULL << PIN_CODEC_PWR) | (1ULL << PIN_PA_EN),
        .mode = GPIO_MODE_OUTPUT,
    };
    gpio_config(&pw);
    gpio_set_level(PIN_SYS_POW, 1);
    gpio_set_level(PIN_CODEC_PWR, 1);
    gpio_set_level(PIN_PA_EN, 0); /* 功放先关着，放提示音时再开 */
#endif

#if PIN_LED != PIN_NC
    /* 普通板子的 LED 是直连的，这里顺手配好 */
    gpio_config_t led = {
        .pin_bit_mask = 1ULL << PIN_LED,
        .mode = GPIO_MODE_OUTPUT,
    };
    gpio_config(&led);
    board_led(false);
#endif
    return ESP_OK;
}

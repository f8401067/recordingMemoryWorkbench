#include "ui.h"

#include <stdio.h>

#include "app_config.h"
#include "board.h"
#include "driver/gpio.h"
#include "driver/ledc.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "ui";

#define BUZZER_LEDC_TIMER LEDC_TIMER_0
#define BUZZER_LEDC_CHAN LEDC_CHANNEL_0
#define BUZZER_DUTY_ON 512 /* 10bit 的一半 */

/* ------------------------------------------------------------------ *
 * 按键
 * ------------------------------------------------------------------ */
static bool s_btn_down;
static uint32_t s_btn_t0;
static bool s_btn_fired;

/* ------------------------------------------------------------------ *
 * LED
 * ------------------------------------------------------------------ */
static ui_led_mode_t s_led_mode = LED_OFF;
static bool s_led_phys;
static uint32_t s_led_next;

/* ------------------------------------------------------------------ *
 * 蜂鸣器
 * ------------------------------------------------------------------ */
typedef struct {
    uint16_t on_ms;
    uint16_t off_ms;
    uint8_t times;
} beep_pat_t;

static const beep_pat_t PATTERNS[] = {
    [SND_NONE] = {0, 0, 0},
    [SND_START] = {70, 60, 2},
    [SND_STOP] = {140, 0, 1},
    [SND_OK] = {50, 50, 3},
    [SND_ERR] = {400, 0, 1},
};

static uint8_t s_beep_left;
static bool s_beep_on;
static uint32_t s_beep_next;
static beep_pat_t s_beep_cur;

static void buzzer_raw(bool on)
{
#if PIN_BUZZER != PIN_NC
    ledc_set_duty(LEDC_LOW_SPEED_MODE, BUZZER_LEDC_CHAN, on ? BUZZER_DUTY_ON : 0);
    ledc_update_duty(LEDC_LOW_SPEED_MODE, BUZZER_LEDC_CHAN);
#else
    (void)on;
#endif
}

static void led_raw(bool on)
{
#if BOARD_HAS_EXIO
    board_led(on); /* 板载灯挂在 AW9523B 上，走 IO 扩展 */
#elif PIN_LED != PIN_NC
    gpio_set_level(PIN_LED, LED_ACTIVE_LOW ? (on ? 0 : 1) : (on ? 1 : 0));
#else
    (void)on;
#endif
}

esp_err_t ui_init(void)
{
#if PIN_BTN != PIN_NC
    gpio_config_t btn = {
        .pin_bit_mask = 1ULL << PIN_BTN,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ONLY,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&btn);
#endif

#if PIN_LED != PIN_NC
    gpio_config_t led = {
        .pin_bit_mask = 1ULL << PIN_LED,
        .mode = GPIO_MODE_OUTPUT,
    };
    gpio_config(&led);
    led_raw(false);
#endif

#if PIN_BUZZER != PIN_NC
    ledc_timer_config_t tcfg = {
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .duty_resolution = LEDC_TIMER_10_BIT,
        .timer_num = BUZZER_LEDC_TIMER,
        .freq_hz = 2700,
        .clk_cfg = LEDC_AUTO_CLK,
    };
    ledc_timer_config(&tcfg);
    ledc_channel_config_t ccfg = {
        .gpio_num = PIN_BUZZER,
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .channel = BUZZER_LEDC_CHAN,
        .timer_sel = BUZZER_LEDC_TIMER,
        .duty = 0,
        .hpoint = 0,
    };
    ledc_channel_config(&ccfg);
#endif

    ESP_LOGI(TAG, "按键 GPIO%d，LED %d，蜂鸣器 %d", (int)PIN_BTN, (int)PIN_LED, (int)PIN_BUZZER);
    return ESP_OK;
}

ui_event_t ui_poll(uint32_t now_ms)
{
#if PIN_BTN == PIN_NC
    return UI_EV_NONE;
#else
    bool down = gpio_get_level(PIN_BTN) == 0; /* 按下接地 */

    if (down && !s_btn_down) {
        if (now_ms - s_btn_t0 < BTN_DEBOUNCE_MS && s_btn_t0 != 0) {
            return UI_EV_NONE; /* 抖动 */
        }
        s_btn_down = true;
        s_btn_t0 = now_ms;
        s_btn_fired = false;
        return UI_EV_NONE;
    }

    if (down && s_btn_down && !s_btn_fired && (now_ms - s_btn_t0) >= BTN_LONG_PRESS_MS) {
        s_btn_fired = true;
        return UI_EV_LONG;
    }

    if (!down && s_btn_down) {
        s_btn_down = false;
        uint32_t held = now_ms - s_btn_t0;
        if (!s_btn_fired && held >= BTN_DEBOUNCE_MS) {
            return UI_EV_SHORT;
        }
    }
    return UI_EV_NONE;
#endif
}

void ui_led(ui_led_mode_t mode)
{
    s_led_mode = mode;
    s_led_next = 0;
    if (mode == LED_OFF) {
        s_led_phys = false;
        led_raw(false);
    } else if (mode == LED_ON) {
        s_led_phys = true;
        led_raw(true);
    }
}

void ui_sound(ui_sound_t s)
{
    if (s == SND_NONE || !PATTERNS[s].times) {
        return;
    }
    s_beep_cur = PATTERNS[s];
    s_beep_left = s_beep_cur.times;
    s_beep_on = true;
    s_beep_next = 0; /* 下一个 tick 立即响 */
    buzzer_raw(true);
}

void ui_tick(uint32_t now_ms)
{
    /* LED 闪烁 */
    if (s_led_mode == LED_BLINK_SLOW || s_led_mode == LED_BLINK_FAST) {
        uint32_t period = (s_led_mode == LED_BLINK_SLOW) ? 1000 : 160;
        if (now_ms >= s_led_next) {
            s_led_phys = !s_led_phys;
            led_raw(s_led_phys);
            s_led_next = now_ms + period / 2;
        }
    }

    /* 蜂鸣器节奏 */
    if (s_beep_left > 0 && now_ms >= s_beep_next) {
        if (s_beep_on) {
            buzzer_raw(false);
            s_beep_on = false;
            s_beep_left--;
            s_beep_next = now_ms + (s_beep_left > 0 ? s_beep_cur.off_ms : 0);
        } else if (s_beep_left > 0) {
            buzzer_raw(true);
            s_beep_on = true;
            s_beep_next = now_ms + s_beep_cur.on_ms;
        }
    }
}

void ui_announce_code(const char *code)
{
#if PIN_BUZZER != PIN_NC
    ESP_LOGI(TAG, "逐字节播报编号（0 用长音表示）");
    for (const char *p = code; *p; p++) {
        int d = *p - '0';
        if (d <= 0) {
            buzzer_raw(true);
            vTaskDelay(pdMS_TO_TICKS(700));
            buzzer_raw(false);
        } else {
            for (int i = 0; i < d; i++) {
                buzzer_raw(true);
                vTaskDelay(pdMS_TO_TICKS(110));
                buzzer_raw(false);
                vTaskDelay(pdMS_TO_TICKS(160));
            }
        }
        vTaskDelay(pdMS_TO_TICKS(800));
    }
#else
    (void)code;
#endif
}

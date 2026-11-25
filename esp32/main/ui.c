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

/* 普通 GPIO 驱动的板载灯用 LEDC 调光（IO 扩展上的灯只能开/关）。
 * 用 timer1/channel1，避开蜂鸣器的 timer0/channel0。 */
#if PIN_LED != PIN_NC && !BOARD_HAS_EXIO
#define LED_LEDC_TIMER LEDC_TIMER_1
#define LED_LEDC_CHAN LEDC_CHANNEL_1
#define LED_DUTY_MAX 1023 /* 10bit */
#endif

/* ------------------------------------------------------------------ *
 * 按键
 * ------------------------------------------------------------------ */
static bool s_btn_down;
static uint32_t s_btn_t0;
static bool s_btn_fired;
static bool s_btn_reset_fired; /* 3 秒的重置手势只报一次 */
static uint8_t s_click_cnt;    /* 连击计数 */
static uint32_t s_click_ms;    /* 最后一次点击的时刻 */

#if PIN_BTN2 != PIN_NC
/* 第二按键（可选，别的板子有）。这块 ePaper 板的 PWR 是电源键，没接 */
static bool s_btn2_down;
static uint32_t s_btn2_t0;
static bool s_btn2_fired;
#endif

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

/* pct = 看起来的亮度 0~100%。低电平点亮的灯要把占空比反过来算：
 * 亮度 100% → 一直拉低；亮度 100% - x → 高电平占 x%，灯只在这 x% 里灭。 */
static void led_set_pct(int pct)
{
#if BOARD_HAS_EXIO
    board_led(pct > 50); /* 扩展芯片上的灯没有 PWM，过半就算开 */
#elif PIN_LED != PIN_NC
    if (pct < 0) {
        pct = 0;
    }
    if (pct > 100) {
        pct = 100;
    }
    int high_pct = LED_ACTIVE_LOW ? (100 - pct) : pct;
    uint32_t duty = (uint32_t)high_pct * LED_DUTY_MAX / 100;
    ledc_set_duty(LEDC_LOW_SPEED_MODE, LED_LEDC_CHAN, duty);
    ledc_update_duty(LEDC_LOW_SPEED_MODE, LED_LEDC_CHAN);
#else
    (void)pct;
#endif
}

static void led_raw(bool on)
{
    led_set_pct(on ? 100 : 0);
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

#if PIN_BTN2 != PIN_NC
    gpio_config_t btn2 = {
        .pin_bit_mask = 1ULL << PIN_BTN2,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ONLY,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&btn2);
#endif

#if PIN_LED != PIN_NC
#if BOARD_HAS_EXIO
    led_raw(false); /* 灯在 IO 扩展上，board_led() 负责，不需要 GPIO/LEDC */
#else
    /* 普通 GPIO 灯：配 LEDC 才能调"微亮"。20kHz 在人耳之外，
     * 也不会被麦克风/ADC 拾成可闻噪声 */
    ledc_timer_config_t ltcfg = {
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .duty_resolution = LEDC_TIMER_10_BIT,
        .timer_num = LED_LEDC_TIMER,
        .freq_hz = 20000,
        .clk_cfg = LEDC_AUTO_CLK,
    };
    ledc_timer_config(&ltcfg);
    ledc_channel_config_t lccfg = {
        .gpio_num = PIN_LED,
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .channel = LED_LEDC_CHAN,
        .timer_sel = LED_LEDC_TIMER,
        .duty = 0,
        .hpoint = 0,
    };
    ledc_channel_config(&lccfg);
    led_raw(false);
#endif
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
#if PIN_BTN2 != PIN_NC
    /* 第二键：按住 2 秒 → 清配网。先判它，免得和录音键抢事件 */
    bool down2 = gpio_get_level(PIN_BTN2) == 0;
    if (down2 && !s_btn2_down) {
        s_btn2_down = true;
        s_btn2_t0 = now_ms;
        s_btn2_fired = false;
    } else if (down2 && s_btn2_down && !s_btn2_fired &&
               (now_ms - s_btn2_t0) >= BTN_RESET_PRESS_MS) {
        s_btn2_fired = true;
        return UI_EV_RESET;
    } else if (!down2 && s_btn2_down) {
        s_btn2_down = false;
    }
#endif

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
        s_btn_reset_fired = false;
        return UI_EV_NONE;
    }

    if (down && s_btn_down && !s_btn_fired && (now_ms - s_btn_t0) >= BTN_LONG_PRESS_MS) {
        s_btn_fired = true;
        return UI_EV_LONG;
    }

    /* 继续按住到 3 秒：重置配网。放在长按之后，所以按住不放会先收到一次
     * UI_EV_LONG（录音中=停止），再收到 UI_EV_RESET。 */
    if (down && s_btn_down && !s_btn_reset_fired &&
        (now_ms - s_btn_t0) >= BTN_RESET_PRESS_MS) {
        s_btn_reset_fired = true;
        return UI_EV_RESET;
    }

    if (!down && s_btn_down) {
        s_btn_down = false;
        uint32_t held = now_ms - s_btn_t0;
        if (!s_btn_fired && held >= BTN_DEBOUNCE_MS) {
            /* 记一次点击，等窗口结束再决定是单/双/三击 */
            s_click_cnt++;
            s_click_ms = now_ms;
        }
    }

    /* 连击判定：窗口结束还没再按就算数。1 下=单击，2 下及以上=双击 */
    if (s_click_cnt > 0 && (now_ms - s_click_ms) > BTN_CLICK_GAP_MS) {
        uint8_t n = s_click_cnt;
        s_click_cnt = 0;
        return (n >= 2) ? UI_EV_DOUBLE : UI_EV_SHORT;
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
    } else if (mode == LED_DIM) {
        s_led_phys = false; /* 不参与闪烁 */
        led_set_pct(LED_DIM_PERCENT);
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

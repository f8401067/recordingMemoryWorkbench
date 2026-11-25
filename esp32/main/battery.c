#include "battery.h"

#include <stdbool.h>

#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_log.h"

static const char *TAG = "batt";

static adc_oneshot_unit_handle_t s_adc;
static adc_cali_handle_t s_cali;
static bool s_ok;

/* 分压比：电池电压 → ADC 引脚电压 = 1:2（官方板级 1/2 分压） */
#define BATT_DIVIDER 2.0f
/* 满电 / 空电电压，取自官方 factory 例程 */
#define BATT_FULL_V 4.12f
#define BATT_EMPTY_V 3.0f

esp_err_t battery_init(void)
{
    if (s_ok) {
        return ESP_OK;
    }

    adc_cali_curve_fitting_config_t cali_cfg = {
        .unit_id = ADC_UNIT_1,
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_12,
    };
    if (adc_cali_create_scheme_curve_fitting(&cali_cfg, &s_cali) != ESP_OK) {
        ESP_LOGW(TAG, "ADC 曲线校准不可用，电量按原始值估算");
        s_cali = NULL;
    }

    adc_oneshot_unit_init_cfg_t unit_cfg = {
        .unit_id = ADC_UNIT_1,
    };
    esp_err_t err = adc_oneshot_new_unit(&unit_cfg, &s_adc);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "ADC1 初始化失败：%s", esp_err_to_name(err));
        return err;
    }

    adc_oneshot_chan_cfg_t chan_cfg = {
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_12,
    };
    err = adc_oneshot_config_channel(s_adc, ADC_CHANNEL_3, &chan_cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "ADC1_CH3 配置失败：%s", esp_err_to_name(err));
        return err;
    }

    s_ok = true;
    ESP_LOGI(TAG, "电池检测就绪（ADC1_CH3，分压 1:2）");
    return ESP_OK;
}

float battery_voltage(void)
{
    if (!s_ok) {
        return 0;
    }
    int raw = 0;
    if (adc_oneshot_read(s_adc, ADC_CHANNEL_3, &raw) != ESP_OK) {
        return 0;
    }
    int mv;
    if (s_cali) {
        if (adc_cali_raw_to_voltage(s_cali, raw, &mv) != ESP_OK) {
            return 0;
        }
    } else {
        mv = raw * 3300 / 4095;
    }
    return 0.001f * (float)mv * BATT_DIVIDER;
}

int battery_percent(void)
{
    float v = battery_voltage();
    if (v <= 0.05f) {
        return -1;
    }
    if (v <= BATT_EMPTY_V) {
        return 0;
    }
    if (v >= BATT_FULL_V) {
        return 100;
    }
    return (int)((v - BATT_EMPTY_V) / (BATT_FULL_V - BATT_EMPTY_V) * 100.0f + 0.5f);
}

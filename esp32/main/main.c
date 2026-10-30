/* 忆录录音笔 · 固件入口
 *
 * ESP32-S3 / ESP32-C3 通用：
 *   I2S 数字麦 → 20ms 帧 → VAD 有人声才编码 → Opus(12kbps) → LittleFS 暂存
 *   → 联网上传 /api/open/ingest → 服务端转写 + 纪要
 */
#include <stdio.h>

#include "app.h"
#include "app_config.h"
#include "audio.h"
#include "board.h"
#include "display.h"
#include "esp_log.h"
#include "esp_pm.h"
#include "netcfg.h"
#include "nvs_flash.h"
#include "ogg_opus.h"
#include "spool.h"
#include "ui.h"
#include "uploader.h"

static const char *TAG = "main";

void app_main(void)
{
    ESP_LOGI(TAG, "忆录录音笔 %s on %s", APP_FW_VERSION, CONFIG_IDF_TARGET);

    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    /* 先配置板级：I2C 总线、IO 扩展（背光/状态灯），屏和音频都靠它 */
    board_init();

    /* 先亮屏、先能响，后面哪一步挂了用户也知道 */
    display_init();
    ui_init();
    uploader_init();

    if (spool_init() != ESP_OK) {
        ESP_LOGE(TAG, "本地存储挂载失败，功能不可用");
    }
    if (ogg_opus_init() != ESP_OK) {
        ESP_LOGE(TAG, "Opus 编码器初始化失败");
    }
    if (audio_init() != ESP_OK) {
        ESP_LOGE(TAG, "I2S 初始化失败，检查麦克风接线");
    }

    /* 动态调频：空闲 80MHz，编码时自动上到 160MHz */
    esp_pm_config_t pm = {
        .max_freq_mhz = 160,
        .min_freq_mhz = 80,
        .light_sleep_enable = false, /* 麦克风要持续采样，不进 light sleep */
    };
    esp_err_t pm_err = esp_pm_configure(&pm);
    if (pm_err != ESP_OK) {
        ESP_LOGW(TAG, "调频配置失败：%s", esp_err_to_name(pm_err));
    }

    /* 没屏幕的设备：开机用蜂鸣器把 6 位编号逐位"念"出来。
     * 还没配 WiFi 时会直接开热点配网，编号在配网页面上也能看到，就不念了 */
    if (!uploader_has_token() && !display_available() && netcfg_has_creds()) {
        ESP_LOGW(TAG, "未配对，设备编号 %s（蜂鸣器播报中）", uploader_code());
        ui_announce_code(uploader_code());
    } else {
        ESP_LOGI(TAG, "设备编号 %s", uploader_code());
    }

    app_run();
}

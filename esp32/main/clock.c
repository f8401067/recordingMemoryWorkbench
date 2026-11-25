#include "clock.h"

#include <stdio.h>
#include <string.h>
#include <time.h>

#include "board.h"
#include "driver/i2c_master.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "nvs.h"

#include "app_config.h"
#include "spool.h"

static const char *TAG = "clock";

/* PCF85063A：秒/分/时/日/周/月/年 都在 0x02~0x08，BCD 编码 */
#define PCF85063_ADDR 0x51
#define PCF_REG_SEC 0x02
#define PCF_OS_BIT 0x80 /* 秒寄存器的 bit7：晶振停过，时间不可信 */

#define NVS_KEY_EPOCH "epoch"
#define MIN_VALID_EPOCH 1735689600u /* 2025-01-01，早于这个必然是没校准的 */

/* 屏幕日期按本地时区显示。recordedAt 仍然用 UTC 上送，由服务端转本地；
 * 这里只影响屏显，中国没有夏令时，固定 +8 小时即可 */
#define DISPLAY_TZ_OFFSET_SEC (8 * 3600)

/* 小于这个量级的跳变只当漂移修正，不动已经录好的分片；大跳变（掉电丢时、
 * 关机一整晚）才需要把待传分片的时间戳整体平移 */
#define SHIFT_MIN_SEC 60

static struct {
    i2c_master_dev_handle_t rtc;
    bool have_base;
    uint32_t base_epoch;
    int64_t base_us;
} C;

/* clock_set 发现大跳变时，直接把待上传分片的时间戳整体平移（见下） */

static uint8_t bcd2bin(uint8_t v)
{
    return (uint8_t)((v >> 4) * 10 + (v & 0x0Fu));
}

static uint8_t bin2bcd(uint8_t v)
{
    return (uint8_t)(((v / 10u) << 4) | (v % 10u));
}

static bool rtc_read(uint32_t *out)
{
    if (!C.rtc) {
        return false;
    }
    uint8_t reg = PCF_REG_SEC;
    uint8_t b[7] = {0};
    if (i2c_master_transmit_receive(C.rtc, &reg, 1, b, sizeof(b), 100) != ESP_OK) {
        return false;
    }
    if (b[0] & PCF_OS_BIT) {
        return false; /* 掉过电，时钟没走住 */
    }
    struct tm t = {0};
    t.tm_sec = bcd2bin(b[0] & 0x7F);
    t.tm_min = bcd2bin(b[1] & 0x7F);
    t.tm_hour = bcd2bin(b[2] & 0x3F);
    t.tm_mday = bcd2bin(b[3] & 0x3F);
    t.tm_wday = bcd2bin(b[4] & 0x07);
    t.tm_mon = (int)bcd2bin(b[5] & 0x1F) - 1;
    t.tm_year = (int)bcd2bin(b[6]) + 100;
    t.tm_isdst = 0;
    time_t e = timegm(&t);
    if (e <= 0) {
        return false;
    }
    *out = (uint32_t)e;
    return true;
}

static bool rtc_write(uint32_t epoch)
{
    if (!C.rtc) {
        return false;
    }
    time_t tt = (time_t)epoch;
    struct tm t;
    gmtime_r(&tt, &t);

    /* Control_1 清零：STOP=0 让时钟继续走，顺便清掉可能的中断标志 */
    uint8_t ctrl[2] = {0x00, 0x00};
    i2c_master_transmit(C.rtc, ctrl, sizeof(ctrl), 100);

    uint8_t b[8] = {
        PCF_REG_SEC,
        bin2bcd((uint8_t)t.tm_sec),
        bin2bcd((uint8_t)t.tm_min),
        bin2bcd((uint8_t)t.tm_hour),
        bin2bcd((uint8_t)t.tm_mday),
        bin2bcd((uint8_t)(t.tm_wday < 0 ? 0 : t.tm_wday)),
        bin2bcd((uint8_t)(t.tm_mon + 1)),
        bin2bcd((uint8_t)(t.tm_year - 100)),
    };
    return i2c_master_transmit(C.rtc, b, sizeof(b), 100) == ESP_OK;
}

static uint32_t nvs_load(void)
{
    nvs_handle_t h;
    uint32_t v = 0;
    if (nvs_open(APP_NVS_NS, NVS_READONLY, &h) == ESP_OK) {
        nvs_get_u32(h, NVS_KEY_EPOCH, &v);
        nvs_close(h);
    }
    return v;
}

static void nvs_store(uint32_t epoch)
{
    nvs_handle_t h;
    if (nvs_open(APP_NVS_NS, NVS_READWRITE, &h) == ESP_OK) {
        nvs_set_u32(h, NVS_KEY_EPOCH, epoch);
        nvs_commit(h);
        nvs_close(h);
    }
}

static void set_base(uint32_t epoch)
{
    C.have_base = true;
    C.base_epoch = epoch;
    C.base_us = esp_timer_get_time();
}

esp_err_t clock_init(void)
{
    i2c_master_bus_handle_t bus = board_i2c_bus();
    /* 开机时 I2C 上还有别的东西在初始化，probe 偶尔会超时（真机上就漏过一次），
     * 重试几次比直接判"没装 RTC"靠谱 */
    bool found = false;
    for (int i = 0; bus && i < 3 && !found; i++) {
        if (i2c_master_probe(bus, PCF85063_ADDR, 50) == ESP_OK) {
            found = true;
        } else {
            vTaskDelay(pdMS_TO_TICKS(30));
        }
    }
    if (found) {
        i2c_device_config_t cfg = {
            .dev_addr_length = I2C_ADDR_BIT_LEN_7,
            .device_address = PCF85063_ADDR,
            .scl_speed_hz = 100000,
        };
        if (i2c_master_bus_add_device(bus, &cfg, &C.rtc) != ESP_OK) {
            C.rtc = NULL;
        }
    } else {
        ESP_LOGW(TAG, "PCF85063（0x51）没响应：板子没装 RTC 或 I2C 引脚不对");
    }

    uint32_t from_rtc = 0;
    bool rtc_ok = rtc_read(&from_rtc);
    if (rtc_ok && from_rtc >= MIN_VALID_EPOCH) {
        set_base(from_rtc);
        ESP_LOGI(TAG, "时间来自 RTC：epoch=%u", (unsigned)from_rtc);
        return ESP_OK;
    }
    if (C.rtc && !rtc_ok) {
        ESP_LOGW(TAG, "PCF85063 有响应但时间无效（掉过电，OS 位=1），等联网校准后写回");
    } else if (C.rtc) {
        ESP_LOGW(TAG, "PCF85063 时间太旧（epoch=%u），等联网校准后写回", (unsigned)from_rtc);
    }

    /* RTC 没有（板子没装）或掉电归零：用上次存的时间兜底。
     * 误差 = 关机时长，但总比"没有时间"强，补传时至少能落到正确的那一天。 */
    uint32_t from_nvs = nvs_load();
    if (from_nvs >= MIN_VALID_EPOCH) {
        set_base(from_nvs);
        if (C.rtc) {
            rtc_write(from_nvs);
        }
        ESP_LOGW(TAG, "时间来自 NVS（上次保存值）：epoch=%u，等待联网校准", (unsigned)from_nvs);
        return ESP_OK;
    }

    ESP_LOGW(TAG, "没有可用时间源，录音暂时不带时间戳（联网后自动校准）");
    return ESP_ERR_NOT_FOUND;
}

bool clock_valid(void)
{
    return C.have_base;
}

uint32_t clock_now(void)
{
    if (!C.have_base) {
        return 0;
    }
    int64_t us = esp_timer_get_time() - C.base_us;
    if (us < 0) {
        return C.base_epoch;
    }
    return C.base_epoch + (uint32_t)(us / 1000000);
}

void clock_set(uint32_t epoch)
{
    if (epoch < MIN_VALID_EPOCH) {
        return;
    }
    uint32_t before = clock_now();
    set_base(epoch);
    if (rtc_write(epoch)) {
        ESP_LOGI(TAG, "RTC 已校准到 %u（原来 %u）", (unsigned)epoch, (unsigned)before);
    }
    nvs_store(epoch);

    /* 旧时钟明显不对：已经录好但还没上传的分片也是按旧时钟盖的章，
     * 现在就把这批 epoch 整体平移过来，否则服务端会按错误时间切会话。
     * 只动当时已存在的文件，校准之后录的分片用的是新时钟，不受影响 */
    if (before >= MIN_VALID_EPOCH) {
        int32_t delta = (int32_t)((int64_t)epoch - (int64_t)before);
        if (delta >= SHIFT_MIN_SEC || delta <= -SHIFT_MIN_SEC) {
            int moved = spool_shift_epochs(delta);
            ESP_LOGW(TAG, "时钟校正 %+d 秒，回填 %d 个待传分片", (int)delta, moved);
        }
    }
}

void clock_persist(void)
{
    uint32_t now = clock_now();
    if (now >= MIN_VALID_EPOCH) {
        nvs_store(now);
    }
}

const char *clock_source(void)
{
    if (!C.have_base) {
        return "-";
    }
    return C.rtc ? "RTC" : "NVS";
}

bool clock_iso8601(uint32_t epoch, char *out, size_t out_len)
{
    if (!epoch || !out || out_len < 21) {
        return false;
    }
    time_t tt = (time_t)epoch;
    struct tm t;
    gmtime_r(&tt, &t);
    return strftime(out, out_len, "%Y-%m-%dT%H:%M:%SZ", &t) > 0;
}

bool clock_local_date(char *out, size_t out_len)
{
    if (!out || out_len < 11) {
        return false;
    }
    uint32_t now = clock_now();
    if (!now) {
        return false; /* 还没校准过，屏上宁可空着也不显示 1970 年 */
    }
    time_t tt = (time_t)(now + DISPLAY_TZ_OFFSET_SEC);
    struct tm t;
    gmtime_r(&tt, &t);
    return strftime(out, out_len, "%Y-%m-%d", &t) > 0;
}

uint32_t clock_parse_http_date(const char *s)
{
    if (!s) {
        return 0;
    }
    /* 只认 RFC 1123 这个最常见格式：Sun, 01 Nov 2026 01:30:00 GMT */
    static const char *mon = "JanFebMarAprMayJunJulAugSepOctNovDec";
    int day = 0, year = 0, hh = 0, mm = 0, ss = 0;
    char mon_s[4] = {0};
    if (sscanf(s, "%*[^,], %d %3s %d %d:%d:%d", &day, mon_s, &year, &hh, &mm, &ss) != 6) {
        return 0;
    }
    const char *p = strstr(mon, mon_s);
    if (!p || ((p - mon) % 3) != 0) {
        return 0;
    }
    struct tm t = {0};
    t.tm_mday = day;
    t.tm_mon = (int)((p - mon) / 3);
    t.tm_year = year - 1900;
    t.tm_hour = hh;
    t.tm_min = mm;
    t.tm_sec = ss;
    time_t e = timegm(&t);
    if (e <= 0) {
        return 0;
    }
    uint32_t v = (uint32_t)e;
    return v >= MIN_VALID_EPOCH ? v : 0;
}

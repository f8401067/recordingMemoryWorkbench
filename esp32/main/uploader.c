#include "uploader.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "app_config.h"
#include "esp_event.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "netcfg.h"
#include "nvs.h"

static const char *TAG = "net";

#define WIFI_BIT_OK BIT0
#define WIFI_BIT_FAIL BIT1

static EventGroupHandle_t s_eg;
static bool s_wifi_inited;
static bool s_wifi_started;
static bool s_suppress_connect; /* 配网期间不希望 STA 自动乱连 */
static char s_code[8];
static char s_token[80];
static char s_server[NETCFG_URL_MAX]; /* NVS 里配的服务端地址，没有就用 menuconfig 默认 */
static int64_t s_last_pair_ms;

/* ------------------------------------------------------------------ *
 * 设备编号：MAC 打散成 6 位数字，跟网页端 README 的算法一致
 * ------------------------------------------------------------------ */
static void make_code(void)
{
    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    uint32_t x = ((uint32_t)mac[2] << 24) | ((uint32_t)mac[3] << 16) | ((uint32_t)mac[4] << 8) | mac[5];
    uint32_t code = (uint32_t)(((uint64_t)x * 2654435761ULL) % 1000000ULL);
    snprintf(s_code, sizeof(s_code), "%06u", (unsigned)code);
}

const char *uploader_code(void)
{
    return s_code;
}

static void load_token(void)
{
    nvs_handle_t h;
    size_t len = sizeof(s_token);
    s_token[0] = '\0';
    if (nvs_open(APP_NVS_NS, NVS_READONLY, &h) == ESP_OK) {
        if (nvs_get_str(h, "token", s_token, &len) != ESP_OK) {
            s_token[0] = '\0';
        }
        nvs_close(h);
    }
}

bool uploader_has_token(void)
{
    return s_token[0] != '\0';
}

void uploader_forget_token(void)
{
    nvs_handle_t h;
    if (nvs_open(APP_NVS_NS, NVS_READWRITE, &h) == ESP_OK) {
        nvs_erase_key(h, "token");
        nvs_commit(h);
        nvs_close(h);
    }
    s_token[0] = '\0';
    ESP_LOGW(TAG, "token 已清除，需要重新配对");
}

esp_err_t uploader_init(void)
{
    make_code();
    load_token();
    netcfg_load_server_url(s_server, sizeof(s_server));
    s_eg = xEventGroupCreate();
    ESP_LOGI(TAG, "设备编号 %s，%s，服务端 %s", s_code, s_token[0] ? "已配对" : "未配对", s_server);
    return ESP_OK;
}

const char *uploader_server_url(void)
{
    return s_server;
}

/* ------------------------------------------------------------------ *
 * WiFi
 * ------------------------------------------------------------------ */
static void wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        if (!s_suppress_connect) {
            esp_wifi_connect();
        }
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_eg, WIFI_BIT_OK);
        if (s_suppress_connect) {
            /* 配网模式：连不上就不管，容易误连到旧 AP 干扰扫描 */
        } else if (!(xEventGroupGetBits(s_eg) & WIFI_BIT_FAIL)) {
            esp_wifi_connect(); /* 还在等第一次连上，继续重试 */
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        xEventGroupClearBits(s_eg, WIFI_BIT_FAIL);
        xEventGroupSetBits(s_eg, WIFI_BIT_OK);
    }
}

void uploader_wifi_suppress_connect(bool on)
{
    s_suppress_connect = on;
}

/* netif / event loop / wifi_init 只能做一次，配网和正常联网都要用 */
esp_err_t uploader_wifi_init_once(void)
{
    if (s_wifi_inited) {
        return ESP_OK;
    }
    esp_err_t err = esp_netif_init();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        return err;
    }
    err = esp_event_loop_create_default();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        return err;
    }
    if (!esp_netif_create_default_wifi_sta()) {
        return ESP_FAIL;
    }

    wifi_init_config_t init_cfg = WIFI_INIT_CONFIG_DEFAULT();
    err = esp_wifi_init(&init_cfg);
    if (err != ESP_OK) {
        return err;
    }
    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event,
                                                       NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event,
                                                       NULL, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    s_wifi_inited = true;
    return ESP_OK;
}

bool uploader_wifi_online(void)
{
    return s_eg && (xEventGroupGetBits(s_eg) & WIFI_BIT_OK);
}

bool uploader_wifi_connect(uint32_t timeout_ms)
{
    if (uploader_wifi_online()) {
        return true;
    }

    char ssid[33];
    char pass[65];
    netcfg_load_creds(ssid, sizeof(ssid), pass, sizeof(pass));
    if (ssid[0] == '\0') {
        ESP_LOGW(TAG, "没配 WiFi：手机连 Recall-%s 打开 http://192.168.4.1 配置", s_code);
        return false;
    }

    if (uploader_wifi_init_once() != ESP_OK) {
        ESP_LOGE(TAG, "wifi 初始化失败");
        return false;
    }
    s_suppress_connect = false;

    wifi_config_t wc = {0};
    /* ssid/pass 都是定长字段，用 memcpy 而不是 snprintf：
     * 32 字节的 SSID 恰好占满时 snprintf 会被 -Wformat-truncation 判定截断 */
    size_t n = strlen(ssid);
    if (n > sizeof(wc.sta.ssid)) {
        n = sizeof(wc.sta.ssid);
    }
    memcpy(wc.sta.ssid, ssid, n);
    n = strlen(pass);
    if (n > sizeof(wc.sta.password)) {
        n = sizeof(wc.sta.password);
    }
    memcpy(wc.sta.password, pass, n);
    wc.sta.threshold.authmode = WIFI_AUTH_OPEN;
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));

    xEventGroupClearBits(s_eg, WIFI_BIT_OK | WIFI_BIT_FAIL);
    if (!s_wifi_started) {
        ESP_ERROR_CHECK(esp_wifi_start());
        s_wifi_started = true;
    } else {
        esp_wifi_connect();
    }
    /* modem sleep：不断开连接的前提下让射频周期性休眠，比常开省一半以上 */
    esp_wifi_set_ps(WIFI_PS_MIN_MODEM);

    ESP_LOGI(TAG, "连接 WiFi %s ...", ssid);
    EventBits_t bits = xEventGroupWaitBits(s_eg, WIFI_BIT_OK | WIFI_BIT_FAIL, pdFALSE, pdFALSE,
                                           pdMS_TO_TICKS(timeout_ms));
    if (bits & WIFI_BIT_OK) {
        ESP_LOGI(TAG, "WiFi 已连接");
        return true;
    }
    ESP_LOGW(TAG, "WiFi 连接超时");
    return false;
}

void uploader_wifi_stop(void)
{
    if (s_wifi_started) {
        esp_wifi_disconnect();
        esp_wifi_stop();
        s_wifi_started = false;
        xEventGroupClearBits(s_eg, WIFI_BIT_OK | WIFI_BIT_FAIL);
        ESP_LOGI(TAG, "WiFi 已关闭");
    }
}

/* ------------------------------------------------------------------ *
 * HTTP 小工具
 * ------------------------------------------------------------------ */
static bool json_str(const char *json, const char *key, char *out, size_t out_sz)
{
    char pat[32];
    snprintf(pat, sizeof(pat), "\"%s\":\"", key);
    const char *p = strstr(json, pat);
    if (!p) {
        return false;
    }
    p += strlen(pat);
    const char *e = strchr(p, '"');
    if (!e) {
        return false;
    }
    size_t n = (size_t)(e - p);
    if (n >= out_sz) {
        n = out_sz - 1;
    }
    memcpy(out, p, n);
    out[n] = '\0';
    return true;
}

static esp_http_client_handle_t mk_client(const char *url, const char *token, int content_len,
                                         bool post)
{
    esp_http_client_config_t cfg = {
        .url = url,
        .timeout_ms = 20000,
        .buffer_size = 1024,
        .buffer_size_tx = 1024,
        .method = post ? HTTP_METHOD_POST : HTTP_METHOD_GET,
        .disable_auto_redirect = true,
    };
    esp_http_client_handle_t c = esp_http_client_init(&cfg);
    if (!c) {
        return NULL;
    }
    if (token && token[0]) {
        esp_http_client_set_header(c, "X-Device-Token", token);
    }
    if (post) {
        esp_http_client_set_header(c, "Content-Type", "application/json");
    }
    (void)content_len;
    return c;
}

/* ------------------------------------------------------------------ *
 * 配对
 * ------------------------------------------------------------------ */
bool uploader_pair(void)
{
    int64_t now = esp_timer_get_time() / 1000;
    if (s_last_pair_ms && now - s_last_pair_ms < 3000) {
        return false;
    }
    s_last_pair_ms = now;

    char url[256];
    snprintf(url, sizeof(url), "%s/api/open/pair?code=%s&name=%s&firmware=%s",
             s_server, s_code, CONFIG_RECALL_DEVICE_NAME, APP_FW_VERSION);

    esp_http_client_handle_t c = mk_client(url, NULL, 0, false);
    if (!c) {
        return false;
    }
    char resp[384] = {0};
    int status = 0;
    if (esp_http_client_open(c, 0) == ESP_OK) {
        esp_http_client_fetch_headers(c);
        status = esp_http_client_get_status_code(c);
        int n = esp_http_client_read(c, resp, sizeof(resp) - 1);
        if (n > 0) {
            resp[n] = '\0';
        }
    }
    esp_http_client_close(c);
    esp_http_client_cleanup(c);

    if (status != 200) {
        ESP_LOGW(TAG, "pair HTTP %d: %s", status, resp);
        return false;
    }

    char token[80];
    if (json_str(resp, "token", token, sizeof(token))) {
        snprintf(s_token, sizeof(s_token), "%s", token);
        nvs_handle_t h;
        if (nvs_open(APP_NVS_NS, NVS_READWRITE, &h) == ESP_OK) {
            nvs_set_str(h, "token", s_token);
            nvs_commit(h);
            nvs_close(h);
        }
        char name[48] = {0};
        json_str(resp, "name", name, sizeof(name));
        ESP_LOGI(TAG, "配对成功，设备名「%s」，token 已存 NVS", name);
        return true;
    }

    ESP_LOGI(TAG, "等待后台绑定，编号 %s", s_code);
    return false;
}

/* ------------------------------------------------------------------ *
 * 上传
 * ------------------------------------------------------------------ */
up_result_t uploader_upload(const spool_item_t *item)
{
    if (!uploader_has_token()) {
        return UP_TOKEN_INVALID;
    }
    if (!uploader_wifi_connect(15000)) {
        return UP_NO_WIFI;
    }

    FILE *fp = fopen(item->path, "rb");
    if (!fp) {
        ESP_LOGW(TAG, "打不开 %s，当作已处理", item->path);
        return UP_OK;
    }
    fseek(fp, 0, SEEK_END);
    long size = ftell(fp);
    fseek(fp, 0, SEEK_SET);

    char url[256];
    snprintf(url, sizeof(url), "%s/api/open/ingest?durationMs=%u&rate=%d&channels=1",
             s_server, (unsigned)item->dur_ms, APP_SAMPLE_RATE);

    up_result_t result = UP_RETRY;
    esp_http_client_handle_t c = mk_client(url, s_token, (int)size, true);
    if (!c) {
        fclose(fp);
        return UP_RETRY;
    }
    esp_http_client_set_header(c, "Content-Type", "audio/ogg");

    if (esp_http_client_open(c, size) == ESP_OK) {
        uint8_t buf[2048];
        size_t n;
        bool write_ok = true;
        while ((n = fread(buf, 1, sizeof(buf), fp)) > 0) {
            if (esp_http_client_write(c, (const char *)buf, (int)n) != (int)n) {
                write_ok = false;
                break;
            }
        }
        if (write_ok) {
            esp_http_client_fetch_headers(c);
            int status = esp_http_client_get_status_code(c);
            char resp[256] = {0};
            int rn = esp_http_client_read(c, resp, sizeof(resp) - 1);
            if (rn > 0) {
                resp[rn] = '\0';
            }
            if (status == 200) {
                result = UP_OK;
                ESP_LOGI(TAG, "已上传 %s (%ld bytes): %s", item->path, size, resp);
            } else if (status == 401) {
                result = UP_TOKEN_INVALID;
                ESP_LOGW(TAG, "token 失效（HTTP 401），需要重新配对");
            } else {
                ESP_LOGW(TAG, "上传失败 HTTP %d: %s", status, resp);
            }
        } else {
            ESP_LOGW(TAG, "上传中断");
        }
    }
    esp_http_client_close(c);
    esp_http_client_cleanup(c);
    fclose(fp);
    return result;
}

bool uploader_close_session(void)
{
    if (!uploader_has_token() || !uploader_wifi_online()) {
        return false;
    }
    char url[192];
    snprintf(url, sizeof(url), "%s/api/open/ingest/close", s_server);

    bool ok = false;
    esp_http_client_handle_t c = mk_client(url, s_token, 0, true);
    if (!c) {
        return false;
    }
    if (esp_http_client_open(c, 0) == ESP_OK) {
        esp_http_client_fetch_headers(c);
        int status = esp_http_client_get_status_code(c);
        char resp[256] = {0};
        int n = esp_http_client_read(c, resp, sizeof(resp) - 1);
        if (n > 0) {
            resp[n] = '\0';
        }
        ok = (status == 200);
        ESP_LOGI(TAG, "收尾 HTTP %d: %s", status, resp);
    }
    esp_http_client_close(c);
    esp_http_client_cleanup(c);
    return ok;
}

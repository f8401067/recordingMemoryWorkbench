#include "uploader.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

#include "app_config.h"
#include "clock.h"
#include "esp_event.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "esp_crt_bundle.h"
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

/* 与服务端的连通性：0 未知 / 1 通 / -1 连不上 / -2 服务端报错。
 * 配对界面用它显示 "SERVER OK" / "NO SERVER" */
static volatile int s_server_state;
int uploader_server_state(void)
{
    return s_server_state;
}

#define WIFI_BIT_OK BIT0
#define WIFI_BIT_FAIL BIT1

static EventGroupHandle_t s_eg;
/* 主任务（配对/配网）和上传任务（CPU1）都会碰 WiFi，必须串行化。
 * 之前没有锁，两边同时 esp_wifi_set_config 会报 "sta is connecting" 然后 abort。 */
static SemaphoreHandle_t s_wifi_lock;
static volatile bool s_paused; /* 配网期间禁止 STA 连接 */
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
    if (!s_wifi_lock) {
        s_wifi_lock = xSemaphoreCreateMutex();
    }
    /* 开机就把 esp_netif / 事件循环 / wifi 建起来。
     * 以前是等第一次 uploader_wifi_connect() 才懒加载，结果"还没联网就调
     * uploader_pair()"时 lwip 的 tcpip_mbox 根本没初始化，
     * esp_http_client 一做 DNS 解析就 assert: Invalid mbox → 无限重启。 */
    if (uploader_wifi_init_once() != ESP_OK) {
        ESP_LOGW(TAG, "网络栈初始化失败，联网功能不可用");
    }
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

bool uploader_wifi_ssid(char *out, size_t n)
{
    if (!out || n == 0) {
        return false;
    }
    out[0] = '\0';

    /* 优先拿真正连着的那个 AP，屏幕要显示的是"连上的 WiFi"而不是保存的配置 */
    wifi_ap_record_t ap;
    if (uploader_wifi_online() && esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
        snprintf(out, n, "%.*s", (int)sizeof(ap.ssid), (const char *)ap.ssid);
        if (out[0]) {
            return true;
        }
    }

    /* 还没连上（例如暂停时）：退回已保存的 SSID，让用户知道要连哪个 */
    char pass[NETCFG_PASS_LEN];
    netcfg_load_creds(out, n, pass, sizeof(pass));
    return out[0] != '\0';
}

/* ------------------------------------------------------------------ *
 * 多网络连接：扫一次 → 在范围内的按信号强弱排前面 → 逐个试
 *
 * 为什么先扫：已保存 3 个网络时，盲试要等每个都超时（3×6 秒），而扫描
 * 一次 1.5 秒就能知道谁在范围内，直接把"不在家"的那些跳过。
 * ------------------------------------------------------------------ */
typedef struct {
    netcfg_ap_t ap;
    int rssi;
    bool seen;
} wifi_cand_t;

#define WIFI_TRY_MS 6000 /* 单个网络最多等多久拿到 IP */

static void scan_fill(wifi_cand_t *c, int n)
{
    static wifi_ap_record_t recs[20];
    wifi_scan_config_t sc = {0};
    uint16_t num = 20;
    if (esp_wifi_scan_start(&sc, true) != ESP_OK) {
        ESP_LOGW(TAG, "扫描失败，按保存顺序依次尝试");
        return;
    }
    if (esp_wifi_scan_get_ap_records(&num, recs) != ESP_OK) {
        return;
    }
    for (int i = 0; i < n; i++) {
        for (int j = 0; j < num; j++) {
            if (recs[j].ssid[0] && strcmp((const char *)recs[j].ssid, c[i].ap.ssid) == 0) {
                if (!c[i].seen || recs[j].rssi > c[i].rssi) {
                    c[i].rssi = recs[j].rssi;
                }
                c[i].seen = true;
            }
        }
    }
}

/* 在范围内的排前面，同组内信号强的优先；没扫到的保持原来的保存顺序 */
static void sort_cands(wifi_cand_t *c, int n)
{
    for (int i = 0; i < n; i++) {
        int best = i;
        for (int j = i + 1; j < n; j++) {
            if (c[j].seen != c[best].seen) {
                if (c[j].seen) {
                    best = j;
                }
            } else if (c[j].rssi > c[best].rssi) {
                best = j;
            }
        }
        if (best != i) {
            wifi_cand_t t = c[i];
            c[i] = c[best];
            c[best] = t;
        }
    }
}

static bool try_one_ap(const netcfg_ap_t *ap, uint32_t wait_ms)
{
    /* 先断开上一次尝试：它可能还卡在 connecting，这时 set_config 会直接报错 */
    esp_wifi_disconnect();
    vTaskDelay(pdMS_TO_TICKS(30));

    wifi_config_t wc = {0};
    /* ssid/pass 都是定长字段，用 memcpy 而不是 snprintf：
     * 32 字节的 SSID 恰好占满时 snprintf 会被 -Wformat-truncation 判定截断 */
    size_t n = strlen(ap->ssid);
    if (n > sizeof(wc.sta.ssid)) {
        n = sizeof(wc.sta.ssid);
    }
    memcpy(wc.sta.ssid, ap->ssid, n);
    n = strlen(ap->pass);
    if (n > sizeof(wc.sta.password)) {
        n = sizeof(wc.sta.password);
    }
    memcpy(wc.sta.password, ap->pass, n);
    wc.sta.threshold.authmode = WIFI_AUTH_OPEN;

    esp_err_t err = esp_wifi_set_config(WIFI_IF_STA, &wc);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "设置 WiFi 配置失败：%s", esp_err_to_name(err));
        return false;
    }

    xEventGroupClearBits(s_eg, WIFI_BIT_OK | WIFI_BIT_FAIL);
    err = esp_wifi_connect();
    if (err != ESP_OK && err != ESP_ERR_WIFI_STATE && err != ESP_ERR_WIFI_CONN) {
        ESP_LOGW(TAG, "发起连接失败：%s", esp_err_to_name(err));
        return false;
    }
    EventBits_t bits = xEventGroupWaitBits(s_eg, WIFI_BIT_OK, pdFALSE, pdFALSE,
                                           pdMS_TO_TICKS(wait_ms));
    return (bits & WIFI_BIT_OK) != 0;
}

static bool wifi_connect_locked(uint32_t timeout_ms)
{
    if (uploader_wifi_online()) {
        return true;
    }

    static wifi_cand_t cands[NETCFG_MAX_AP];
    int n = 0;
    for (int i = 0; i < NETCFG_MAX_AP; i++) {
        if (!netcfg_ap_at(i, &cands[n].ap)) {
            break;
        }
        cands[n].rssi = -127;
        cands[n].seen = false;
        n++;
    }
    if (n == 0) {
        ESP_LOGW(TAG, "没配 WiFi：手机连 Recall-%s 打开 http://192.168.4.1 配置", s_code);
        return false;
    }

    if (uploader_wifi_init_once() != ESP_OK) {
        ESP_LOGE(TAG, "wifi 初始化失败");
        return false;
    }

    /* 连接时机由这里自己控制：不然 STA_START 事件会用上一次的配置抢先连一个 */
    s_suppress_connect = true;
    if (!s_wifi_started) {
        if (esp_wifi_start() != ESP_OK) {
            ESP_LOGW(TAG, "启动 WiFi 失败");
            s_suppress_connect = false;
            return false;
        }
        s_wifi_started = true;
    }

    if (n > 1) {
        scan_fill(cands, n);
        sort_cands(cands, n);
    }

    int64_t deadline = esp_timer_get_time() / 1000 + (int64_t)timeout_ms;
    bool ok = false;
    for (int i = 0; i < n && !ok; i++) {
        int64_t left = deadline - esp_timer_get_time() / 1000;
        if (left <= 800) {
            break;
        }
        uint32_t wait = (uint32_t)(left < WIFI_TRY_MS ? left : WIFI_TRY_MS);
        ESP_LOGI(TAG, "试连 %s（%s %d dBm）", cands[i].ap.ssid,
                 cands[i].seen ? "在范围内" : "没扫到", cands[i].rssi);
        ok = try_one_ap(&cands[i].ap, wait);
        if (ok) {
            /* 连上就把它提到首位：下次优先试这个，正好覆盖"出门/回家"的切换 */
            netcfg_ap_promote(cands[i].ap.ssid);
            ESP_LOGI(TAG, "WiFi 已连接：%s", cands[i].ap.ssid);
        }
    }
    s_suppress_connect = false;

    if (!ok) {
        ESP_LOGW(TAG, "已保存的 %d 个 WiFi 都连不上", n);
        return false;
    }
    /* modem sleep：不断开连接的前提下让射频周期性休眠，比常开省一半以上 */
    esp_wifi_set_ps(WIFI_PS_MIN_MODEM);
    return true;
}

bool uploader_wifi_connect(uint32_t timeout_ms)
{
    if (s_paused) {
        return false;
    }
    bool was_online = uploader_wifi_online();
    if (s_wifi_lock) {
        xSemaphoreTake(s_wifi_lock, portMAX_DELAY);
    }
    bool ok = wifi_connect_locked(timeout_ms);
    if (s_wifi_lock) {
        xSemaphoreGive(s_wifi_lock);
    }
    /* 每次真正从"没网"连上网络都顺手对一次时（配对、补传、单文件上传都走这里）。
     * 掉电后开机时间是从 NVS 估出来的，只有联网时才可能拨正；补传前必须校准，
     * 否则离线录的分片会按旧时间上送。放在锁外做，HTTP 请求不占 WiFi 互斥锁 */
    if (ok && !was_online) {
        uploader_sync_clock();
    }
    return ok;
}

void uploader_pause(bool on)
{
    s_paused = on;
    if (on) {
        uploader_wifi_stop();
    }
}

void uploader_wifi_stop(void)
{
    if (s_wifi_lock) {
        xSemaphoreTake(s_wifi_lock, portMAX_DELAY);
    }
    if (s_wifi_started) {
        esp_wifi_disconnect();
        esp_wifi_stop();
        s_wifi_started = false;
        xEventGroupClearBits(s_eg, WIFI_BIT_OK | WIFI_BIT_FAIL);
        ESP_LOGI(TAG, "WiFi 已关闭");
    }
    if (s_wifi_lock) {
        xSemaphoreGive(s_wifi_lock);
    }
}

/* ------------------------------------------------------------------ *
 * HTTP 小工具
 * ------------------------------------------------------------------ */
static bool json_str(const char *json, const char *key, char *out, size_t out_sz){
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

/* ------------------------------------------------------------------ *
 * 顺带对时
 *
 * 每个 HTTP 响应都带 Date 头，是现成的、不依赖外网 NTP 的时间源。
 * 每次联网都会调用（见 uploader_wifi_connect），所以这里不做"差 30 秒才算"
 * 的死区——那会让 RTC 的日常漂移一直累积。留 2 秒只是避开 Date 头
 * 只精确到秒带来的反复写盘。
 * ------------------------------------------------------------------ */
static void sync_clock_from_header(const char *date)
{
    uint32_t remote = clock_parse_http_date(date);
    if (!remote) {
        return;
    }
    uint32_t local = clock_now();
    uint32_t diff;
    if (local == 0) {
        diff = 0xFFFFFFFFu;
    } else {
        diff = (remote > local) ? (remote - local) : (local - remote);
    }
    if (diff > 2) {
        clock_set(remote);
    }
}

static esp_err_t header_event(esp_http_client_event_t *e)
{
    if (e->event_id == HTTP_EVENT_ON_HEADER && e->header_key && e->header_value) {
        if (strcasecmp(e->header_key, "Date") == 0) {
            sync_clock_from_header(e->header_value);
        }
    }
    return ESP_OK;
}

static esp_http_client_handle_t mk_client(const char *url, const char *token, int content_len,
                                         bool post)
{
    /* 所有 HTTP 都从这里出去，所以把关口放在这：没连上网一律不发。
     * 联网前做 DNS 解析会在 lwip 里 assert(Invalid mbox) 把设备打进重启循环。 */
    if (!uploader_wifi_online()) {
        ESP_LOGD(TAG, "还没联网，跳过请求 %s", url);
        return NULL;
    }
    esp_http_client_config_t cfg = {
        .url = url,
        .timeout_ms = 20000,
        .buffer_size = 1024,
        .buffer_size_tx = 1024,
        .method = post ? HTTP_METHOD_POST : HTTP_METHOD_GET,
        .disable_auto_redirect = true,
        /* 支持 https://：用 IDF 内置根证书包校验服务端证书。
         * 本项目没开 MBEDTLS_HAVE_TIME_DATE，所以不校验证书有效期，
         * 也就不需要 SNTP 对时就能直接握手。 */
        .crt_bundle_attach = esp_crt_bundle_attach,
        .event_handler = header_event,
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
 * 上传用的会话级连接
 *
 * 一次"上传轮次"里往往有好几个分片。同一个服务器、同一个 TLS 连接连着发，
 * 后面的文件就不用再握手了（公网 HTTPS 下一次握手 ~200-400ms）。
 * ------------------------------------------------------------------ */
static esp_http_client_handle_t s_sess;
static char s_sess_resp[512];
static int s_sess_resp_len;

static esp_err_t sess_event(esp_http_client_event_t *e)
{
    header_event(e);
    if (e->event_id == HTTP_EVENT_ON_DATA && e->data_len > 0) {
        int room = (int)sizeof(s_sess_resp) - 1 - s_sess_resp_len;
        if (room > 0) {
            int take = e->data_len < room ? e->data_len : room;
            memcpy(s_sess_resp + s_sess_resp_len, e->data, (size_t)take);
            s_sess_resp_len += take;
            s_sess_resp[s_sess_resp_len] = '\0';
        }
    }
    return ESP_OK;
}

static esp_http_client_handle_t sess_client(const char *url)
{
    if (!s_sess) {
        esp_http_client_config_t cfg = {
            .url = url,
            .timeout_ms = 20000,
            .buffer_size = 2048,
            .buffer_size_tx = 2048,
            .method = HTTP_METHOD_POST,
            .disable_auto_redirect = true,
            .keep_alive_enable = true,
            .crt_bundle_attach = esp_crt_bundle_attach,
            .event_handler = sess_event,
        };
        s_sess = esp_http_client_init(&cfg);
    } else {
        esp_http_client_set_url(s_sess, url);
    }
    return s_sess;
}

/* 一轮传完 / 关射频前调用：把连接彻底放掉 */
void uploader_session_end(void)
{
    if (s_sess) {
        esp_http_client_cleanup(s_sess);
        s_sess = NULL;
    }
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
    /* 没连上网就别发 HTTP：lwip 没准备好的时候 esp_http_client 会在
     * getaddrinfo 里 assert（tcpip_send_msg_wait_sem: Invalid mbox）。
     * 这一道是最后防线，正常的调用方应该先 uploader_wifi_connect() 再调这里。 */
    if (!uploader_wifi_online()) {
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

    if (status == 0) {
        s_server_state = -1; /* 连不上：DNS/网络/TLS 握手失败 */
    } else if (status != 200) {
        s_server_state = -2; /* 连上了但服务端报错 */
    } else {
        s_server_state = 1;
    }
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
/* 联网后先发一个最轻的请求，只为拿 Date 头把时钟校准。
 * 补传场景下分片的 epoch 是离线时盖的，必须在列目录/上传之前校准，
 * 否则会有一片按旧时间上送、被服务端切成另一条会话。 */
bool uploader_sync_clock(void)
{
    if (!uploader_wifi_online()) {
        return false;
    }
    esp_http_client_handle_t c = mk_client(s_server, NULL, 0, false);
    if (!c) {
        return false;
    }
    esp_err_t err = esp_http_client_perform(c);
    esp_http_client_cleanup(c);
    return err == ESP_OK;
}

/* ISO8601 里的冒号在 URL 查询串里转义一下，免得被当成 scheme 分隔符 */
static void iso_to_query(char *dst, size_t n, const char *src)
{
    size_t o = 0;
    for (const char *p = src; *p && o + 4 < n; p++) {
        if (*p == ':') {
            memcpy(dst + o, "%3A", 3);
            o += 3;
        } else {
            dst[o++] = *p;
        }
    }
    dst[o] = '\0';
}

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
        /* 文件可能刚被空间回收删掉。不能报 UP_OK，否则调用方会当成功删记录 */
        ESP_LOGW(TAG, "打不开 %s，下次再试", item->path);
        return UP_RETRY;
    }
    fseek(fp, 0, SEEK_END);
    long size = ftell(fp);
    fseek(fp, 0, SEEK_SET);
    if (size <= 0 || size > 256 * 1024) {
        fclose(fp);
        ESP_LOGW(TAG, "文件大小异常（%ld），跳过", size);
        return UP_RETRY;
    }
    uint8_t *buf = malloc((size_t)size);
    if (!buf) {
        fclose(fp);
        return UP_RETRY;
    }
    size_t got = fread(buf, 1, (size_t)size, fp);
    fclose(fp);
    if (got != (size_t)size) {
        free(buf);
        return UP_RETRY;
    }

    /* 带上这一片"什么时候录的"。设备攒了一整天再补传时，服务端靠它还原
     * 真实的时间线并正确切分会话；时钟不可用就不带，服务端退回按到达时间算。 */
    char iso[24] = {0};
    char when[64] = {0};
    if (item->epoch && clock_iso8601(item->epoch, iso, sizeof(iso))) {
        char esc[32];
        iso_to_query(esc, sizeof(esc), iso);
        snprintf(when, sizeof(when), "&recordedAt=%s", esc);
    }

    char url[320];
    snprintf(url, sizeof(url), "%s/api/open/ingest?durationMs=%u&rate=%d&channels=1%s",
             s_server, (unsigned)item->dur_ms, APP_SAMPLE_RATE, when);

    s_sess_resp_len = 0;
    s_sess_resp[0] = '\0';

    up_result_t result = UP_RETRY;
    esp_http_client_handle_t c = sess_client(url);
    if (!c) {
        free(buf);
        return UP_RETRY;
    }
    char chunk_key[40];
    snprintf(chunk_key, sizeof(chunk_key), "%s-%u-%u", s_code, (unsigned)item->seq,
             (unsigned)item->bytes);
    esp_http_client_set_header(c, "X-Device-Token", s_token);
    esp_http_client_set_header(c, "Content-Type", "audio/ogg");
    esp_http_client_set_header(c, "X-Chunk-Id", chunk_key);
    esp_http_client_set_method(c, HTTP_METHOD_POST);
    esp_http_client_set_post_field(c, (const char *)buf, (int)size);

    esp_err_t err = esp_http_client_perform(c);
    int status = (err == ESP_OK) ? esp_http_client_get_status_code(c) : 0;
    free(buf);

    if (err != ESP_OK) {
        ESP_LOGW(TAG, "上传失败：%s", esp_err_to_name(err));
        uploader_session_end(); /* 连接可能已经坏了，下次重建 */
        s_server_state = -1;
        return UP_RETRY;
    }
    if (status == 200) {
        s_server_state = 1;
        result = UP_OK;
        ESP_LOGI(TAG, "已上传 %s (%ld bytes): %s", item->path, size, s_sess_resp);
    } else if (status == 401) {
        result = UP_TOKEN_INVALID;
        ESP_LOGW(TAG, "token 失效（HTTP 401），需要重新配对");
    } else {
        s_server_state = (status >= 500) ? -2 : -2;
        ESP_LOGW(TAG, "上传失败 HTTP %d: %s", status, s_sess_resp);
    }
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

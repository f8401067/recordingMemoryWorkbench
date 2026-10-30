#include "netcfg.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "app_config.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include "uploader.h"

static const char *TAG = "netcfg";

static volatile bool s_saved;
static esp_netif_t *s_ap_netif; /* 只创建一次：重复 create 会 assert */

/* ------------------------------------------------------------------ *
 * 凭据读写
 * ------------------------------------------------------------------ */
static bool nvs_get_str_safe(const char *key, char *out, size_t out_sz)
{
    nvs_handle_t h;
    size_t len = out_sz;
    out[0] = '\0';
    if (nvs_open(APP_NVS_NS, NVS_READONLY, &h) != ESP_OK) {
        return false;
    }
    bool ok = nvs_get_str(h, key, out, &len) == ESP_OK && out[0];
    nvs_close(h);
    return ok;
}

bool netcfg_has_creds(void)
{
    char ssid[33];
    if (nvs_get_str_safe("wifi_ssid", ssid, sizeof(ssid))) {
        return true;
    }
    return CONFIG_RECALL_WIFI_SSID[0] != '\0';
}

void netcfg_load_creds(char *ssid, size_t ssid_sz, char *pass, size_t pass_sz)
{
    if (nvs_get_str_safe("wifi_ssid", ssid, ssid_sz)) {
        if (!nvs_get_str_safe("wifi_pass", pass, pass_sz)) {
            pass[0] = '\0';
        }
        return;
    }
    snprintf(ssid, ssid_sz, "%s", CONFIG_RECALL_WIFI_SSID);
    snprintf(pass, pass_sz, "%s", CONFIG_RECALL_WIFI_PASSWORD);
}

void netcfg_save_creds(const char *ssid, const char *pass)
{
    nvs_handle_t h;
    if (nvs_open(APP_NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
        return;
    }
    nvs_set_str(h, "wifi_ssid", ssid);
    nvs_set_str(h, "wifi_pass", pass ? pass : "");
    nvs_commit(h);
    nvs_close(h);
    ESP_LOGI(TAG, "WiFi 已保存：%s", ssid);
}

void netcfg_clear_creds(void)
{
    nvs_handle_t h;
    if (nvs_open(APP_NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
        return;
    }
    nvs_erase_key(h, "wifi_ssid");
    nvs_erase_key(h, "wifi_pass");
    nvs_commit(h);
    nvs_close(h);
    ESP_LOGW(TAG, "WiFi 凭据已清除");
}

/* ------------------------------------------------------------------ *
 * 服务端地址：NVS 优先，没配过才用 menuconfig 的默认值
 * ------------------------------------------------------------------ */
void netcfg_load_server_url(char *out, size_t out_sz)
{
    if (!nvs_get_str_safe("srv_url", out, out_sz)) {
        snprintf(out, out_sz, "%s", CONFIG_RECALL_SERVER_URL);
    }
    /* 去掉结尾斜杠：拼接时统一写成 "%s/api/open/..." */
    size_t n = strlen(out);
    while (n > 0 && out[n - 1] == '/') {
        out[--n] = '\0';
    }
}

void netcfg_save_server_url(const char *url)
{
    char tmp[NETCFG_URL_MAX];
    snprintf(tmp, sizeof(tmp), "%s", url ? url : "");
    size_t n = strlen(tmp);
    while (n > 0 && tmp[n - 1] == '/') {
        tmp[--n] = '\0';
    }
    if (!n) {
        return; /* 空值不覆盖，保持原样 */
    }

    nvs_handle_t h;
    if (nvs_open(APP_NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
        return;
    }
    nvs_set_str(h, "srv_url", tmp);
    nvs_commit(h);
    nvs_close(h);
    ESP_LOGI(TAG, "服务端地址已保存：%s", tmp);
}

/* ------------------------------------------------------------------ *
 * 网页
 * ------------------------------------------------------------------ */
/* 页面分三段拼起来：CSS 里到处是 %，整段当格式串用会被编译器当成
 * 非法转换符（%; 之类）直接报错，所以固定文本一律走 %s 参数。 */
static const char PAGE_A[] =
    "<!doctype html><meta charset=utf-8>"
    "<meta name=viewport content='width=device-width,initial-scale=1'>"
    "<title>忆录录音笔 · 配网</title>"
    "<style>"
    "body{margin:0;padding:24px 18px;font:16px/1.6 -apple-system,system-ui,sans-serif;"
    "background:#111;color:#eee}"
    "h1{font-size:19px;margin:0 0 4px}"
    ".sub{color:#888;font-size:13px;margin-bottom:22px}"
    "label{display:block;font-size:13px;color:#9aa;margin:18px 0 6px}"
    "select,input{width:100%;box-sizing:border-box;padding:12px;font-size:16px;"
    "border-radius:10px;border:1px solid #333;background:#1c1c1e;color:#eee}"
    "button{width:100%;margin-top:26px;padding:14px;font-size:17px;border:0;"
    "border-radius:12px;background:#2f7cf6;color:#fff;font-weight:600}"
    "button:disabled{background:#333;color:#777}"
    "#msg{margin-top:16px;font-size:14px;color:#8fd}"
    ".tip{margin-top:26px;font-size:12px;color:#666;line-height:1.7}"
    "</style>"
    "<h1>忆录录音笔</h1>"
    "<div class=sub>连上家里的 WiFi（只支持 2.4GHz），顺便确认服务器地址</div>"
    "<label>WiFi 名称</label><select id=ssid><option>正在扫描…</option></select>"
    "<label>密码</label><input id=pass type=password placeholder='没有密码就留空'>"
    "<label>服务器地址</label><input id=srv inputmode=url autocapitalize=off "
    "autocorrect=off spellcheck=false placeholder='http://192.168.1.10:8787'>"
    "<button id=btn>保存并重启</button><div id=msg></div>"
    "<div class=tip>设备编号 ";

static const char PAGE_B[] = " · 固件 ";

static const char PAGE_C[] =
    "<br>保存后设备会自动重启并尝试连接；连不上就再进一次配网。</div>"
    "<script>"
    "const sel=document.getElementById('ssid'),btn=document.getElementById('btn'),"
    "msg=document.getElementById('msg'),srv=document.getElementById('srv');"
    /* 先把当前服务端地址填进输入框，用户照着改个 IP 就行 */
    "fetch('/cfg').then(r=>r.json()).then(d=>{if(d.srv)srv.value=d.srv})"
    ".catch(()=>{});"
    "fetch('/scan').then(r=>r.json()).then(d=>{"
    " if(!d.aps.length){sel.innerHTML='<option value=\"\">没扫到热点</option>';return}"
    " sel.innerHTML=d.aps.map(a=>'<option value=\"'+a.s+'\">'+a.s+' ('+a.r+'dBm)'"
    " +(a.l?' 🔒':'')+'</option>').join('')})"
    ".catch(()=>{sel.innerHTML='<option value=\"\">扫描失败</option>'});"
    "btn.onclick=()=>{"
    " if(!srv.value.trim()){msg.textContent='请填服务器地址';return}"
    " btn.disabled=true;msg.textContent='正在保存…';"
    " fetch('/save',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},"
    " body:'ssid='+encodeURIComponent(sel.value)"
    " +'&pass='+encodeURIComponent(document.getElementById('pass').value)"
    " +'&srv='+encodeURIComponent(srv.value)})"
    ".then(r=>r.text()).then(t=>{msg.textContent=t})"
    ".catch(()=>{btn.disabled=false;msg.textContent='保存失败，重试一次'})};"
    "</script>";

static esp_err_t h_index(httpd_req_t *req)
{
    static char page[3072];
    int n = snprintf(page, sizeof(page), "%s%s%s%s%s", PAGE_A, uploader_code(), PAGE_B,
                     APP_FW_VERSION, PAGE_C);
    if (n < 0) {
        n = 0;
    } else if (n >= (int)sizeof(page)) {
        n = (int)sizeof(page) - 1;
    }
    httpd_resp_set_type(req, "text/html; charset=utf-8");
    return httpd_resp_send(req, page, n);
}

/* SSID 里可能有引号/反斜杠/中文（JSON 里必须转义或原样 UTF-8 直出） */
static void jesc(const char *in, char *out, size_t out_sz)
{
    size_t o = 0;
    for (const unsigned char *p = (const unsigned char *)in; *p && o + 3 < out_sz; p++) {
        if (*p == '"' || *p == '\\') {
            out[o++] = '\\';
            out[o++] = (char)*p;
        } else if (*p < 0x20) {
            continue;
        } else {
            out[o++] = (char)*p;
        }
    }
    out[o] = '\0';
}

/* 当前配置：页面打开时用它把服务端地址预填进输入框 */
static esp_err_t h_cfg(httpd_req_t *req)
{
    char srv[NETCFG_URL_MAX];
    netcfg_load_server_url(srv, sizeof(srv));
    static char json[256];
    int n = snprintf(json, sizeof(json), "{\"srv\":\"%s\",\"code\":\"%s\",\"fw\":\"%s\"}", srv,
                     uploader_code(), APP_FW_VERSION);
    if (n < 0) {
        n = 0;
    } else if (n >= (int)sizeof(json)) {
        n = (int)sizeof(json) - 1;
    }
    httpd_resp_set_type(req, "application/json");
    return httpd_resp_send(req, json, n);
}

static esp_err_t h_scan(httpd_req_t *req)
{
    wifi_scan_config_t scan = {.show_hidden = 0};
    static char json[1536];
    int n = 0;
    n += snprintf(json + n, sizeof(json) - n, "{\"aps\":[");

    bool first = true;
    if (esp_wifi_scan_start(&scan, true) == ESP_OK) {
        uint16_t num = 12;
        static wifi_ap_record_t recs[12];
        if (esp_wifi_scan_get_ap_records(&num, recs) == ESP_OK) {
            for (int i = 0; i < num && n < (int)sizeof(json) - 100; i++) {
                if (!recs[i].ssid[0]) {
                    continue;
                }
                /* 同名 AP 只留一个（取先出现即信号最强的） */
                bool dup = false;
                for (int j = 0; j < i; j++) {
                    if (strcmp((const char *)recs[j].ssid, (const char *)recs[i].ssid) == 0) {
                        dup = true;
                        break;
                    }
                }
                if (dup) {
                    continue;
                }
                char name[80];
                jesc((const char *)recs[i].ssid, name, sizeof(name));
                n += snprintf(json + n, sizeof(json) - n, "%s{\"s\":\"%s\",\"r\":%d,\"l\":%d}",
                              first ? "" : ",", name, recs[i].rssi,
                              recs[i].authmode != WIFI_AUTH_OPEN);
                first = false;
            }
        }
    }
    n += snprintf(json + n, sizeof(json) - n, "]}");
    httpd_resp_set_type(req, "application/json");
    return httpd_resp_send(req, json, n);
}

static void url_decode(char *s)
{
    char *o = s;
    for (char *p = s; *p; p++) {
        if (*p == '+') {
            *o++ = ' ';
        } else if (*p == '%' && p[1] && p[2]) {
            char hex[3] = {p[1], p[2], 0};
            *o++ = (char)strtol(hex, NULL, 16);
            p += 2;
        } else {
            *o++ = *p;
        }
    }
    *o = '\0';
}

/* 从 application/x-www-form-urlencoded 里取一个字段，不破坏原串 */
static void form_get(const char *body, const char *key, char *out, size_t out_sz)
{
    out[0] = '\0';
    size_t klen = strlen(key);
    const char *p = body;
    while ((p = strstr(p, key)) != NULL) {
        /* 必须是字段开头（串首或紧跟 &），避免 pass= 匹配到 ap_pass= */
        if (p == body || p[-1] == '&') {
            break;
        }
        p += klen;
    }
    if (!p) {
        return;
    }
    p += klen;
    const char *e = strchr(p, '&');
    size_t n = e ? (size_t)(e - p) : strlen(p);
    if (n >= out_sz) {
        n = out_sz - 1;
    }
    memcpy(out, p, n);
    out[n] = '\0';
    url_decode(out);
}

static esp_err_t h_save(httpd_req_t *req)
{
    char body[1024] = {0};
    int len = req->content_len;
    if (len <= 0 || len >= (int)sizeof(body)) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "body too large");
        return ESP_FAIL;
    }
    int got = 0;
    while (got < len) {
        int r = httpd_req_recv(req, body + got, len - got);
        if (r <= 0) {
            httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "recv failed");
            return ESP_FAIL;
        }
        got += r;
    }

    char ssid[33] = {0}, pass[65] = {0}, srv[NETCFG_URL_MAX] = {0};
    form_get(body, "ssid=", ssid, sizeof(ssid));
    form_get(body, "pass=", pass, sizeof(pass));
    form_get(body, "srv=", srv, sizeof(srv));

    httpd_resp_set_type(req, "text/plain; charset=utf-8");
    if (!ssid[0]) {
        httpd_resp_sendstr(req, "没选 WiFi，重新选一个");
        return ESP_OK;
    }
    if (pass[0] && strlen(pass) < 8) {
        /* WPA2 密码最短 8 位，短了基本是手滑，提前拦一下省得反复进配网 */
        httpd_resp_sendstr(req, "密码要么留空（开放网络），要么至少 8 位");
        return ESP_OK;
    }
    /* 没带 srv（比如页面被缓存过）就沿用原来的，不做校验 */
    if (srv[0] && strncmp(srv, "http://", 7) != 0 && strncmp(srv, "https://", 8) != 0) {
        httpd_resp_sendstr(req, "服务器地址要以 http:// 开头");
        return ESP_OK;
    }

    netcfg_save_creds(ssid, pass);
    netcfg_save_server_url(srv);

    httpd_resp_sendstr(req, "已保存，设备正在重启…");
    /* 给响应留点时间发出去 */
    vTaskDelay(pdMS_TO_TICKS(400));
    s_saved = true;
    return ESP_OK;
}

/* ------------------------------------------------------------------ *
 * 热点 + 阻塞等待
 * ------------------------------------------------------------------ */
bool netcfg_portal(uint32_t timeout_ms)
{
    s_saved = false;

    uploader_wifi_suppress_connect(true);
    esp_err_t err = uploader_wifi_init_once();
    if (err != ESP_OK) {
        uploader_wifi_suppress_connect(false);
        ESP_LOGE(TAG, "wifi 初始化失败：%s", esp_err_to_name(err));
        return false;
    }
    uploader_wifi_stop();

    /* AP netif 只能建一次，第二次调用直接 assert（实测踩过） */
    if (!s_ap_netif) {
        s_ap_netif = esp_netif_create_default_wifi_ap();
        if (!s_ap_netif) {
            uploader_wifi_suppress_connect(false);
            ESP_LOGE(TAG, "AP netif 创建失败");
            return false;
        }
    }

    wifi_config_t ap = {0};
    snprintf((char *)ap.ap.ssid, sizeof(ap.ap.ssid), "Recall-%s", uploader_code());
    ap.ap.ssid_len = (uint8_t)strlen((const char *)ap.ap.ssid);
    ap.ap.channel = 1;
    ap.ap.max_connection = 2;
    if (CONFIG_RECALL_PROV_AP_PASSWORD[0]) {
        snprintf((char *)ap.ap.password, sizeof(ap.ap.password), "%s",
                 CONFIG_RECALL_PROV_AP_PASSWORD);
        ap.ap.authmode = WIFI_AUTH_WPA2_PSK;
    } else {
        ap.ap.authmode = WIFI_AUTH_OPEN;
    }

    err = esp_wifi_set_mode(WIFI_MODE_APSTA);
    if (err == ESP_OK) {
        err = esp_wifi_set_config(WIFI_IF_AP, &ap);
    }
    if (err == ESP_OK) {
        err = esp_wifi_start();
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "热点启动失败：%s", esp_err_to_name(err));
        esp_wifi_stop();
        uploader_wifi_suppress_connect(false);
        return false;
    }

    httpd_handle_t server = NULL;
    httpd_config_t hcfg = HTTPD_DEFAULT_CONFIG();
    hcfg.server_port = 80;
    hcfg.lru_purge_enable = true;
    hcfg.max_uri_handlers = 8;
    /* LWIP_MAX_SOCKETS=8，httpd 自己要占 3 个，留给客户端的最多 5 个（配网 4 个足够） */
    hcfg.max_open_sockets = 4;
    if (httpd_start(&server, &hcfg) != ESP_OK) {
        ESP_LOGE(TAG, "httpd 启动失败");
        esp_wifi_stop();
        uploader_wifi_suppress_connect(false);
        return false;
    }
    httpd_uri_t u_index = {.uri = "/", .method = HTTP_GET, .handler = h_index};
    httpd_uri_t u_cfg = {.uri = "/cfg", .method = HTTP_GET, .handler = h_cfg};
    httpd_uri_t u_scan = {.uri = "/scan", .method = HTTP_GET, .handler = h_scan};
    httpd_uri_t u_save = {.uri = "/save", .method = HTTP_POST, .handler = h_save};
    httpd_register_uri_handler(server, &u_index);
    httpd_register_uri_handler(server, &u_cfg);
    httpd_register_uri_handler(server, &u_scan);
    httpd_register_uri_handler(server, &u_save);

    ESP_LOGW(TAG, "配网热点已开启：Recall-%s，浏览器打开 http://192.168.4.1", uploader_code());

    uint32_t waited = 0;
    while (!s_saved && waited < timeout_ms) {
        vTaskDelay(pdMS_TO_TICKS(200));
        waited += 200;
    }

    httpd_stop(server);
    esp_wifi_stop();
    esp_wifi_set_mode(WIFI_MODE_STA);
    uploader_wifi_suppress_connect(false);

    if (!s_saved) {
        ESP_LOGW(TAG, "配网超时（%u 秒）", (unsigned)(timeout_ms / 1000));
    }
    return s_saved;
}

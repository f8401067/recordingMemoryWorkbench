#include "netcfg.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "app_config.h"
#include "dns_redirect.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lwip/sockets.h"
#include "nvs.h"
#include "uploader.h"

static const char *TAG = "netcfg";

static volatile bool s_saved;
static volatile bool s_factory;
static esp_netif_t *s_ap_netif; /* 只创建一次：重复 create 会 assert */
static char s_portal_url[48];  /* 形如 http://192.168.4.1/ ，给重定向用 */

/* ------------------------------------------------------------------ *
 * NVS 小工具
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

static void slot_key(char *buf, size_t sz, int idx, const char *suffix)
{
    snprintf(buf, sz, "wifi%d_%s", idx, suffix);
}

/* 用户长按"重置配网"过：即使编译期写了默认 SSID，也不再自动拿它连，
 * 否则清完 NVS 又会被 menuconfig 里的默认值悄悄接回去 */
static bool wifi_force_off(void)
{
    nvs_handle_t h;
    uint8_t off = 0;
    if (nvs_open(APP_NVS_NS, NVS_READONLY, &h) == ESP_OK) {
        nvs_get_u8(h, "wifi_off", &off);
        nvs_close(h);
    }
    return off != 0;
}

/* 列表里存了几组（只数 NVS，不含编译期默认值） */
static int stored_count(void)
{
    nvs_handle_t h;
    uint8_t n = 0;
    if (nvs_open(APP_NVS_NS, NVS_READONLY, &h) != ESP_OK) {
        return 0;
    }
    if (nvs_get_u8(h, "wifi_n", &n) != ESP_OK) {
        n = 0;
    }
    nvs_close(h);
    return n > NETCFG_MAX_AP ? NETCFG_MAX_AP : (int)n;
}

static bool stored_at(int idx, netcfg_ap_t *out)
{
    char ks[16], kp[16];
    slot_key(ks, sizeof(ks), idx, "ssid");
    slot_key(kp, sizeof(kp), idx, "pass");
    memset(out, 0, sizeof(*out));
    if (!nvs_get_str_safe(ks, out->ssid, sizeof(out->ssid))) {
        return false;
    }
    if (!nvs_get_str_safe(kp, out->pass, sizeof(out->pass))) {
        out->pass[0] = '\0';
    }
    return true;
}

/* 老固件只存一组 wifi_ssid / wifi_pass，第一次读的时候搬进列表 */
static void migrate_legacy(void)
{
    if (stored_count() > 0) {
        return;
    }
    char ssid[NETCFG_SSID_LEN];
    if (!nvs_get_str_safe("wifi_ssid", ssid, sizeof(ssid))) {
        return;
    }
    char pass[NETCFG_PASS_LEN];
    if (!nvs_get_str_safe("wifi_pass", pass, sizeof(pass))) {
        pass[0] = '\0';
    }

    nvs_handle_t h;
    if (nvs_open(APP_NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
        return;
    }
    nvs_set_str(h, "wifi0_ssid", ssid);
    nvs_set_str(h, "wifi0_pass", pass);
    nvs_set_u8(h, "wifi_n", 1);
    nvs_commit(h);
    nvs_close(h);
    ESP_LOGI(TAG, "老固件的 WiFi 凭据已并入多网络列表：%s", ssid);
}

static bool store_list(const netcfg_ap_t *list, int n)
{
    nvs_handle_t h;
    if (nvs_open(APP_NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
        return false;
    }
    for (int i = 0; i < NETCFG_MAX_AP; i++) {
        char ks[16], kp[16];
        slot_key(ks, sizeof(ks), i, "ssid");
        slot_key(kp, sizeof(kp), i, "pass");
        if (i < n) {
            nvs_set_str(h, ks, list[i].ssid);
            nvs_set_str(h, kp, list[i].pass);
        } else {
            nvs_erase_key(h, ks);
            nvs_erase_key(h, kp);
        }
    }
    nvs_set_u8(h, "wifi_n", (uint8_t)(n < 0 ? 0 : n));
    /* 列表空了 = 用户把网络都删了，别再退回 menuconfig 里的默认 SSID */
    nvs_set_u8(h, "wifi_off", n > 0 ? 0 : 1);

    /* 老固件只认 wifi_ssid / wifi_pass，这里跟着首位同步一份：
     * 万一刷回旧固件，至少还能连最常用的那个网络 */
    if (n > 0) {
        nvs_set_str(h, "wifi_ssid", list[0].ssid);
        nvs_set_str(h, "wifi_pass", list[0].pass);
    } else {
        nvs_erase_key(h, "wifi_ssid");
        nvs_erase_key(h, "wifi_pass");
    }
    nvs_commit(h);
    nvs_close(h);
    return true;
}

static int load_list(netcfg_ap_t *list, int max)
{
    int n = stored_count();
    int out = 0;
    for (int i = 0; i < n && out < max; i++) {
        if (stored_at(i, &list[out])) {
            out++;
        }
    }
    return out;
}

/* ------------------------------------------------------------------ *
 * 多网络列表
 * ------------------------------------------------------------------ */
int netcfg_ap_count(void)
{
    migrate_legacy();
    int n = stored_count();
    if (n > 0) {
        return n;
    }
    /* 列表是空的：还允许退回 menuconfig 里写死的那个（除非用户主动重置过） */
    if (wifi_force_off() || CONFIG_RECALL_WIFI_SSID[0] == '\0') {
        return 0;
    }
    return 1;
}

bool netcfg_ap_at(int idx, netcfg_ap_t *out)
{
    if (!out || idx < 0) {
        return false;
    }
    migrate_legacy();
    int n = stored_count();
    if (idx < n) {
        return stored_at(idx, out);
    }
    if (idx == 0 && n == 0 && !wifi_force_off() && CONFIG_RECALL_WIFI_SSID[0]) {
        memset(out, 0, sizeof(*out));
        snprintf(out->ssid, sizeof(out->ssid), "%s", CONFIG_RECALL_WIFI_SSID);
        snprintf(out->pass, sizeof(out->pass), "%s", CONFIG_RECALL_WIFI_PASSWORD);
        return true;
    }
    return false;
}

bool netcfg_has_creds(void)
{
    return netcfg_ap_count() > 0;
}

bool netcfg_ap_add(const char *ssid, const char *pass)
{
    if (!ssid || !ssid[0]) {
        return false;
    }
    migrate_legacy();

    netcfg_ap_t list[NETCFG_MAX_AP];
    int n = load_list(list, NETCFG_MAX_AP);

    int found = -1;
    for (int i = 0; i < n; i++) {
        if (strcmp(list[i].ssid, ssid) == 0) {
            found = i;
            break;
        }
    }
    if (found >= 0) {
        /* 同一个网络：只改密码，位置不动（它可能正是最常用的那个） */
        snprintf(list[found].pass, sizeof(list[found].pass), "%s", pass ? pass : "");
    } else {
        if (n >= NETCFG_MAX_AP) {
            /* 满了：挤掉最久没连上的（排在末尾那个），新的放在末尾 */
            memmove(&list[0], &list[1], sizeof(list[0]) * (NETCFG_MAX_AP - 1));
            n = NETCFG_MAX_AP - 1;
        }
        snprintf(list[n].ssid, sizeof(list[n].ssid), "%s", ssid);
        snprintf(list[n].pass, sizeof(list[n].pass), "%s", pass ? pass : "");
        n++;
    }

    bool ok = store_list(list, n);
    if (ok) {
        ESP_LOGI(TAG, "WiFi 已保存（共 %d 组）：%s", n, ssid);
    }
    return ok;
}

bool netcfg_ap_del(const char *ssid)
{
    if (!ssid || !ssid[0]) {
        return false;
    }
    netcfg_ap_t list[NETCFG_MAX_AP];
    int n = load_list(list, NETCFG_MAX_AP);

    int out = 0;
    bool hit = false;
    for (int i = 0; i < n; i++) {
        if (strcmp(list[i].ssid, ssid) == 0) {
            hit = true;
            continue;
        }
        if (out != i) {
            list[out] = list[i];
        }
        out++;
    }
    if (!hit) {
        return false;
    }
    bool ok = store_list(list, out);
    if (ok) {
        ESP_LOGW(TAG, "WiFi 已删除：%s（还剩 %d 组）", ssid, out);
    }
    return ok;
}

void netcfg_ap_promote(const char *ssid)
{
    if (!ssid || !ssid[0]) {
        return;
    }
    netcfg_ap_t list[NETCFG_MAX_AP];
    int n = load_list(list, NETCFG_MAX_AP);

    int idx = -1;
    for (int i = 1; i < n; i++) {
        if (strcmp(list[i].ssid, ssid) == 0) {
            idx = i;
            break;
        }
    }
    if (idx < 1) {
        return; /* 本来就在首位，或不在列表里（编译期默认值） */
    }
    netcfg_ap_t hit = list[idx];
    memmove(&list[1], &list[0], sizeof(list[0]) * (size_t)idx);
    list[0] = hit;
    store_list(list, n);
    ESP_LOGI(TAG, "「%s」提到首选", ssid);
}

void netcfg_load_creds(char *ssid, size_t ssid_sz, char *pass, size_t pass_sz)
{
    netcfg_ap_t ap;
    if (netcfg_ap_at(0, &ap)) {
        snprintf(ssid, ssid_sz, "%s", ap.ssid);
        snprintf(pass, pass_sz, "%s", ap.pass);
        return;
    }
    ssid[0] = '\0';
    pass[0] = '\0';
}

void netcfg_clear_creds(void)
{
    nvs_handle_t h;
    if (nvs_open(APP_NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
        return;
    }
    for (int i = 0; i < NETCFG_MAX_AP; i++) {
        char ks[16], kp[16];
        slot_key(ks, sizeof(ks), i, "ssid");
        slot_key(kp, sizeof(kp), i, "pass");
        nvs_erase_key(h, ks);
        nvs_erase_key(h, kp);
    }
    nvs_erase_key(h, "wifi_n");
    nvs_erase_key(h, "wifi_ssid");
    nvs_erase_key(h, "wifi_pass");
    nvs_set_u8(h, "wifi_off", 1); /* 主动重置：别再用编译期默认 SSID */
    nvs_commit(h);
    nvs_close(h);
    ESP_LOGW(TAG, "WiFi 凭据已全部清除");
}

bool netcfg_factory_requested(void)
{
    return s_factory;
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
    "body{margin:0;padding:24px 18px 40px;font:16px/1.6 -apple-system,system-ui,sans-serif;"
    "background:#111;color:#eee}"
    "h1{font-size:19px;margin:0 0 4px}"
    ".sub{color:#888;font-size:13px;margin-bottom:20px}"
    "label{display:block;font-size:13px;color:#9aa;margin:18px 0 6px}"
    "select,input{width:100%;box-sizing:border-box;padding:12px;font-size:16px;"
    "border-radius:10px;border:1px solid #333;background:#1c1c1e;color:#eee}"
    "button{width:100%;margin-top:22px;padding:14px;font-size:17px;border:0;"
    "border-radius:12px;background:#2f7cf6;color:#fff;font-weight:600}"
    "button:disabled{background:#333;color:#777}"
    "#saved .row{display:flex;align-items:center;gap:10px;padding:10px 12px;margin-bottom:8px;"
    "background:#1c1c1e;border:1px solid #2a2a2c;border-radius:10px;font-size:15px}"
    "#saved .row span{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}"
    "#saved .del{width:auto;margin:0;padding:6px 12px;font-size:13px;"
    "background:#2a2a2c;color:#f88;border-radius:8px}"
    ".empty{color:#666;font-size:13px}"
    "#msg{margin-top:16px;font-size:14px;color:#8fd}"
    ".tip{margin-top:24px;font-size:12px;color:#666;line-height:1.7}"
    ".fac{margin-top:32px;padding-top:16px;border-top:1px solid #262628}"
    ".fac button{background:#2a1a1c;color:#f77;border:1px solid #4a262a;"
    "font-size:14px;font-weight:500}"
    "</style>"
    "<h1>忆录录音笔</h1>"
    "<div class=sub>可以存多组 WiFi，出门到别的网络会自动挑能用的那个</div>"
    "<label>已保存的网络</label><div id=saved><div class=empty>还没有保存过</div></div>"
    "<label>添加 / 修改 WiFi（只支持 2.4GHz）</label>"
    "<select id=ssid><option>正在扫描…</option></select>"
    "<label>密码</label><input id=pass type=password placeholder='没有密码就留空'>"
    "<label>服务器地址</label><input id=srv inputmode=url autocapitalize=off "
    "autocorrect=off spellcheck=false placeholder='http://192.168.1.10:8787'>"
    "<button id=btn>保存并重启</button><div id=msg></div>"
    "<div class=tip>设备编号 ";

static const char PAGE_B[] = " · 固件 ";

static const char PAGE_C[] =
    "<br>保存后设备会自动重启并尝试连接；连不上就再进一次配网。</div>"
    "<div class=fac><button id=fac>恢复出厂设置（清空配网 + 解除绑定）</button></div>"
    "<script>"
    "const $=id=>document.getElementById(id);"
    "const sel=$('ssid'),btn=$('btn'),msg=$('msg'),srv=$('srv'),saved=$('saved'),fac=$('fac');"
    "function render(list){saved.innerHTML='';"
    " if(!list||!list.length){const e=document.createElement('div');"
    "  e.className='empty';e.textContent='还没有保存过';saved.appendChild(e);return}"
    " list.forEach((a,i)=>{const row=document.createElement('div');row.className='row';"
    "  const sp=document.createElement('span');sp.textContent=(i===0?'★ ':'')+a.s;"
    "  const b=document.createElement('button');b.className='del';b.textContent='删除';"
    "  b.onclick=()=>{if(!confirm('删除 '+a.s+' ?'))return;b.disabled=true;"
    "   fetch('/del',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},"
    "   body:'ssid='+encodeURIComponent(a.s)}).then(r=>r.json())"
    "   .then(d=>render(d.saved)).catch(()=>{b.disabled=false})};"
    "  row.appendChild(sp);row.appendChild(b);saved.appendChild(row)})}"
    "fetch('/cfg').then(r=>r.json()).then(d=>{if(d.srv)srv.value=d.srv;render(d.saved)})"
    ".catch(()=>{});"
    "fetch('/scan').then(r=>r.json()).then(d=>{"
    " sel.innerHTML='';"
    " if(!d.aps.length){const o=document.createElement('option');o.value='';"
    "  o.textContent='没扫到热点';sel.appendChild(o);return}"
    " d.aps.forEach(a=>{const o=document.createElement('option');o.value=a.s;"
    "  o.textContent=a.s+' ('+a.r+'dBm)'+(a.l?' [加密]':'');sel.appendChild(o)})})"
    ".catch(()=>{sel.innerHTML='<option value=\"\">扫描失败</option>'});"
    "btn.onclick=()=>{"
    " if(!srv.value.trim()){msg.textContent='请填服务器地址';return}"
    " if(!sel.value){msg.textContent='先选一个 WiFi';return}"
    " btn.disabled=true;msg.textContent='正在保存…';"
    " fetch('/save',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},"
    " body:'ssid='+encodeURIComponent(sel.value)"
    " +'&pass='+encodeURIComponent($('pass').value)"
    " +'&srv='+encodeURIComponent(srv.value)})"
    ".then(r=>r.text()).then(t=>{msg.textContent=t})"
    ".catch(()=>{btn.disabled=false;msg.textContent='保存失败，重试一次'})};"
    "fac.onclick=()=>{if(!confirm('会清空所有 WiFi 和配对信息，确定？'))return;"
    " fac.disabled=true;msg.textContent='正在恢复出厂…';"
    " fetch('/factory',{method:'POST'}).then(r=>r.text()).then(t=>{msg.textContent=t})"
    ".catch(()=>{fac.disabled=false})};"
    "</script>";

static esp_err_t h_index(httpd_req_t *req)
{
    static char page[6144];
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

/* 已保存列表，配网页刷新 / 删除后用 */
static int saved_json(char *buf, size_t sz)
{
    int n = snprintf(buf, sz, "[");
    int cnt = netcfg_ap_count();
    netcfg_ap_t ap;
    bool first = true;
    for (int i = 0; i < cnt; i++) {
        if (!netcfg_ap_at(i, &ap)) {
            break;
        }
        char name[100];
        jesc(ap.ssid, name, sizeof(name));
        n += snprintf(buf + n, sz - (size_t)n, "%s{\"s\":\"%s\"}", first ? "" : ",", name);
        first = false;
        if (n >= (int)sz - 8) {
            break;
        }
    }
    n += snprintf(buf + n, sz - (size_t)n, "]");
    return n;
}

/* 当前配置：页面打开时用它把服务端地址预填进输入框、列出已保存网络 */
static esp_err_t h_cfg(httpd_req_t *req)
{
    char srv[NETCFG_URL_MAX];
    netcfg_load_server_url(srv, sizeof(srv));
    static char json[640];
    char list[360];
    saved_json(list, sizeof(list));
    int n = snprintf(json, sizeof(json), "{\"srv\":\"%s\",\"code\":\"%s\",\"fw\":\"%s\",\"saved\":%s}",
                     srv, uploader_code(), APP_FW_VERSION, list);
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
    n += snprintf(json + n, sizeof(json) - (size_t)n, "{\"aps\":[");

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
                n += snprintf(json + n, sizeof(json) - (size_t)n, "%s{\"s\":\"%s\",\"r\":%d,\"l\":%d}",
                              first ? "" : ",", name, recs[i].rssi,
                              recs[i].authmode != WIFI_AUTH_OPEN);
                first = false;
            }
        }
    }
    n += snprintf(json + n, sizeof(json) - (size_t)n, "]}");
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

/* 读 form body（配网页只有几个字段，一次读完就够） */
static int read_body(httpd_req_t *req, char *body, size_t sz)
{
    int len = req->content_len;
    if (len <= 0 || len >= (int)sz) {
        return -1;
    }
    int got = 0;
    while (got < len) {
        int r = httpd_req_recv(req, body + got, len - got);
        if (r <= 0) {
            return -1;
        }
        got += r;
    }
    body[got] = '\0';
    return got;
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
    if (read_body(req, body, sizeof(body)) < 0) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "body too large");
        return ESP_FAIL;
    }

    char ssid[NETCFG_SSID_LEN] = {0}, pass[NETCFG_PASS_LEN] = {0}, srv[NETCFG_URL_MAX] = {0};
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

    /* 追加而不是替换：之前存过的家里 / 公司 WiFi 都保留 */
    netcfg_ap_add(ssid, pass);
    netcfg_save_server_url(srv);

    httpd_resp_sendstr(req, "已保存，设备正在重启…");
    /* 给响应留点时间发出去 */
    vTaskDelay(pdMS_TO_TICKS(400));
    s_saved = true;
    return ESP_OK;
}

/* 删除某一组网络：返回剩下的列表，页面就地重画，不用重启 */
static esp_err_t h_del(httpd_req_t *req)
{
    char body[256] = {0};
    char ssid[NETCFG_SSID_LEN] = {0};
    if (read_body(req, body, sizeof(body)) > 0) {
        form_get(body, "ssid=", ssid, sizeof(ssid));
    }
    if (ssid[0]) {
        netcfg_ap_del(ssid);
    }

    static char json[512];
    char list[360];
    saved_json(list, sizeof(list));
    int n = snprintf(json, sizeof(json), "{\"saved\":%s}", list);
    if (n < 0) {
        n = 0;
    } else if (n >= (int)sizeof(json)) {
        n = (int)sizeof(json) - 1;
    }
    httpd_resp_set_type(req, "application/json");
    return httpd_resp_send(req, json, n);
}

/* 恢复出厂：清 WiFi + 解绑。设备可以随人换，不必再让服务端手动重置 token */
static esp_err_t h_factory(httpd_req_t *req)
{
    httpd_resp_set_type(req, "text/plain; charset=utf-8");
    httpd_resp_sendstr(req, "已恢复出厂，设备正在重启…");
    vTaskDelay(pdMS_TO_TICKS(400));

    netcfg_clear_creds();
    uploader_forget_token();
    s_factory = true;
    s_saved = true; /* 让 portal 循环退出，交给上层重启 */
    return ESP_OK;
}

/* ------------------------------------------------------------------ *
 * Captive Portal：手机一连热点就自动弹配网页
 * ------------------------------------------------------------------ *
 * 原理：手机连上 WiFi 后会偷偷访问各家厂商的"联网探测"地址来判断这个网
 * 能不能上网，例如
 *   iOS      captive.apple.com/hotspot-detect.html
 *   Android  connectivitycheck.gstatic.com/generate_204
 *   Windows  www.msftncsi.com/ncsi.txt
 * 配合 dns_redirect 把这些域名全部解析到热点自己（192.168.4.1），请求就都
 * 落到我们的 httpd 上；这里统一回 303 重定向到配网首页，系统就会认为
 * "这个网需要登录"，自动弹出认证窗口。
 *
 * 注意 iOS：只回 302 没有响应体的话它不认，必须在响应里带点内容。
 */
static const char CAPTIVE_BODY[] =
    "<!doctype html><meta charset=utf-8>"
    "<title>忆录录音笔 · 配网</title>"
    "<meta http-equiv=refresh content='0;url=/'>"
    "<p>正在打开配网页面… <a href='/'>直接点这里</a>";

static esp_err_t h_captive(httpd_req_t *req)
{
    httpd_resp_set_status(req, "303 See Other");
    httpd_resp_set_hdr(req, "Location", s_portal_url[0] ? s_portal_url : "/");
    httpd_resp_set_type(req, "text/html; charset=utf-8");
    return httpd_resp_send(req, CAPTIVE_BODY, HTTPD_RESP_USE_STRLEN);
}

/* 404 的错误处理函数签名多一个 err 参数，单独包一层 */
static esp_err_t h_captive_404(httpd_req_t *req, httpd_err_code_t err)
{
    (void)err;
    return h_captive(req);
}

/* 各家系统的探测地址。注册成 URI 是为了让这些请求也走一次明确的 handler，
 * 没列到的地址还有下面的 404 handler 兜底，一样会被重定向。 */
static const char *const PROBE_URIS[] = {
    "/hotspot-detect.html",       /* iOS / macOS */
    "/library/test/success.html", /* iOS 老版本 */
    "/generate_204",              /* Android */
    "/gen_204",                   /* Android / Chrome */
    "/mobile/status.php",         /* Android 老版本 */
    "/ncsi.txt",                  /* Windows */
    "/connecttest.txt",           /* Windows 10/11 */
    "/redirect",                  /* Windows */
    "/success.txt",               /* NetworkManager / Ubuntu */
    "/canonical.html",            /* NetworkManager */
    "/favicon.ico",               /* 免得浏览器对着门户页反复 404 */
};

/* ------------------------------------------------------------------ *
 * 热点 + 阻塞等待
 * ------------------------------------------------------------------ */
bool netcfg_portal(uint32_t timeout_ms)
{
    s_saved = false;
    s_factory = false;

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

    /* 热点 IP 拿到手才能开 DNS 劫持：所有域名都要回答成这个地址 */
    esp_netif_ip_info_t ap_ip = {0};
    for (int i = 0; i < 20; i++) {
        if (esp_netif_get_ip_info(s_ap_netif, &ap_ip) == ESP_OK && ap_ip.ip.addr != 0) {
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(100));
    }
    snprintf(s_portal_url, sizeof(s_portal_url), "http://" IPSTR "/", IP2STR(&ap_ip.ip));

    /* 让 DHCP 把 DNS 服务器也发成热点自己。有些手机只认 DHCP 下发的 DNS，
     * 不下发的话它可能会去问 8.8.8.8，劫持就失效了。 */
    esp_netif_dns_info_t dns = {0};
    dns.ip.type = ESP_IPADDR_TYPE_V4;
    dns.ip.u_addr.ip4.addr = ap_ip.ip.addr;
    if (esp_netif_set_dns_info(s_ap_netif, ESP_NETIF_DNS_MAIN, &dns) != ESP_OK) {
        ESP_LOGW(TAG, "设置 DHCP DNS 失败，手机可能不会走劫持");
    }

    dns_redirect_set_ip(ap_ip.ip.addr);
    if (!dns_redirect_start("WIFI_AP_DEF")) {
        ESP_LOGW(TAG, "DNS 劫持没起来，只能手动在浏览器里输入 %s", s_portal_url);
    }

    httpd_handle_t server = NULL;
    httpd_config_t hcfg = HTTPD_DEFAULT_CONFIG();
    hcfg.server_port = 80;
    hcfg.lru_purge_enable = true;
    hcfg.max_uri_handlers = 20;
    /* LWIP_MAX_SOCKETS=8，httpd 自己要占 3 个，留给客户端的最多 5 个（配网 4 个足够） */
    hcfg.max_open_sockets = 4;
    if (httpd_start(&server, &hcfg) != ESP_OK) {
        ESP_LOGE(TAG, "httpd 启动失败");
        dns_redirect_stop();
        esp_wifi_stop();
        uploader_wifi_suppress_connect(false);
        return false;
    }
    httpd_uri_t u_index = {.uri = "/", .method = HTTP_GET, .handler = h_index};
    httpd_uri_t u_cfg = {.uri = "/cfg", .method = HTTP_GET, .handler = h_cfg};
    httpd_uri_t u_scan = {.uri = "/scan", .method = HTTP_GET, .handler = h_scan};
    httpd_uri_t u_save = {.uri = "/save", .method = HTTP_POST, .handler = h_save};
    httpd_uri_t u_del = {.uri = "/del", .method = HTTP_POST, .handler = h_del};
    httpd_uri_t u_fac = {.uri = "/factory", .method = HTTP_POST, .handler = h_factory};
    httpd_register_uri_handler(server, &u_index);
    httpd_register_uri_handler(server, &u_cfg);
    httpd_register_uri_handler(server, &u_scan);
    httpd_register_uri_handler(server, &u_save);
    httpd_register_uri_handler(server, &u_del);
    httpd_register_uri_handler(server, &u_fac);

    for (size_t i = 0; i < sizeof(PROBE_URIS) / sizeof(PROBE_URIS[0]); i++) {
        httpd_uri_t u = {.uri = PROBE_URIS[i], .method = HTTP_GET, .handler = h_captive};
        httpd_register_uri_handler(server, &u);
    }
    /* 兜底：任何没注册过的路径（比如探测地址换了名字）都重定向到首页 */
    httpd_register_err_handler(server, HTTPD_404_NOT_FOUND, h_captive_404);

    ESP_LOGW(TAG, "配网热点已开启：Recall-%s，手机连上会自动弹出配网页（也可手动打开 %s）",
             uploader_code(), s_portal_url);

    uint32_t waited = 0;
    while (!s_saved && waited < timeout_ms) {
        vTaskDelay(pdMS_TO_TICKS(200));
        waited += 200;
    }

    /* 顺序很重要：先停 DNS 劫持，再停 httpd，最后关 WiFi。
     * 反过来的话手机还在往 53 端口发查询，却没人应答，会卡在"正在验证网络"。 */
    dns_redirect_stop();
    httpd_stop(server);
    esp_wifi_stop();
    esp_wifi_set_mode(WIFI_MODE_STA);
    uploader_wifi_suppress_connect(false);

    if (!s_saved) {
        ESP_LOGW(TAG, "配网超时（%u 秒）", (unsigned)(timeout_ms / 1000));
    }
    return s_saved;
}

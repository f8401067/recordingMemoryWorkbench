/* WiFi 配网 + 服务端地址配置
 *
 * 支持保存多组 WiFi（家里 / 公司 / 手机热点…）：设备连网时先扫一次，
 * 挑"在范围内且信号最好"的那个试，连上后把它提到列表首位。出门换环境
 * 不用重新配网，只要那个网络之前存过。
 *
 * 两种途径：
 *   1. 编译期默认值：menuconfig 里填 SSID/密码/服务端地址（适合自己用，改一次就够）
 *   2. 现场配网：设备开热点 Recall-XXXXXX，手机连上开 http://192.168.4.1，
 *      填 WiFi + 服务端地址，保存进 NVS 后重启。NVS 里的值优先于 menuconfig。
 *
 * 进入配网的方式：开机没凭据时自动进；或者待命时连点 3 下 / 长按 3 秒按键。
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* 最多记几组 WiFi。NVS 每个 key 都要占空间，4 组够覆盖"家/公司/热点/备用" */
#define NETCFG_MAX_AP 4
#define NETCFG_SSID_LEN 33
#define NETCFG_PASS_LEN 65

typedef struct {
    char ssid[NETCFG_SSID_LEN];
    char pass[NETCFG_PASS_LEN];
} netcfg_ap_t;

/* 有没有可用的 WiFi（列表非空，或还允许用编译期默认值） */
bool netcfg_has_creds(void);

/* 已保存的网络：idx 0 是最常成功连上的那个 */
int netcfg_ap_count(void);
bool netcfg_ap_at(int idx, netcfg_ap_t *out);

/* 追加或更新（同 SSID 只改密码，不动位置）。列表满了挤掉最末尾那个 */
bool netcfg_ap_add(const char *ssid, const char *pass);
bool netcfg_ap_del(const char *ssid);

/* 连上某个网络后调到首位，下次优先试它 */
void netcfg_ap_promote(const char *ssid);

/* 兼容单网络接口：拿第 0 组 */
void netcfg_load_creds(char *ssid, size_t ssid_sz, char *pass, size_t pass_sz);

/* 清空全部网络（并记住"用户主动重置过"，不再用编译期默认 SSID） */
void netcfg_clear_creds(void);

/* 服务端地址上限（含结尾 '\0'）。配网页面上可改，存在 NVS 里 */
#define NETCFG_URL_MAX 128

/* 读服务端地址：NVS > menuconfig 默认值，结尾斜杠会被去掉 */
void netcfg_load_server_url(char *out, size_t out_sz);

/* 写服务端地址：空串忽略（不覆盖已有配置） */
void netcfg_save_server_url(const char *url);

/* 开热点 + 网页配网，阻塞直到保存成功或超时。返回 true 表示已保存（调用方应重启） */
bool netcfg_portal(uint32_t timeout_ms);

/* 上次 netcfg_portal 期间用户点了"恢复出厂"（凭据和 token 都已清掉） */
bool netcfg_factory_requested(void);

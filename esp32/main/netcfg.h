/* WiFi 配网 + 服务端地址配置
 *
 * 两种途径：
 *   1. 编译期默认值：menuconfig 里填 SSID/密码/服务端地址（适合自己用，改一次就够）
 *   2. 现场配网：设备开热点 Recall-XXXXXX，手机连上开 http://192.168.4.1，
 *      填 WiFi + 服务端地址，保存进 NVS 后重启。NVS 里的值优先于 menuconfig。
 *
 * 进入配网的方式：开机没凭据时自动进；或者待命时连点 3 下按键。
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* NVS 里有没有存过 WiFi */
bool netcfg_has_creds(void);

/* 读凭据：先看 NVS，没有再退回 menuconfig 的默认值 */
void netcfg_load_creds(char *ssid, size_t ssid_sz, char *pass, size_t pass_sz);

void netcfg_save_creds(const char *ssid, const char *pass);
void netcfg_clear_creds(void);

/* 服务端地址上限（含结尾 '\0'）。配网页面上可改，存在 NVS 里 */
#define NETCFG_URL_MAX 128

/* 读服务端地址：NVS > menuconfig 默认值，结尾斜杠会被去掉 */
void netcfg_load_server_url(char *out, size_t out_sz);

/* 写服务端地址：空串忽略（不覆盖已有配置） */
void netcfg_save_server_url(const char *url);

/* 开热点 + 网页配网，阻塞直到保存成功或超时。返回 true 表示已保存（调用方应重启） */
bool netcfg_portal(uint32_t timeout_ms);

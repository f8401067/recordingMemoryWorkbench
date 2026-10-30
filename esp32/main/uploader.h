/* 联网：WiFi 连接、配对、推流、收尾
 *
 * 只跟服务端的 /api/open/ 系列接口打交道（详见项目根 README 的「硬件设备接入」）：
 *   GET  /api/open/pair?code=903471&name=...   → 未绑定 pending，已绑定下发 token
 *   POST /api/open/ingest?durationMs=...       → 推一段音频（audio/ogg）
 *   POST /api/open/ingest/close                → 立刻收尾，触发转写 + 纪要
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "spool.h"

typedef enum {
    UP_OK = 0,        /* 上传成功 */
    UP_RETRY,         /* 网络/服务器打嗝，文件留着下次再传 */
    UP_TOKEN_INVALID, /* token 被换发或设备被删，需要重新配对 */
    UP_NO_WIFI,       /* 连不上 WiFi */
} up_result_t;

esp_err_t uploader_init(void);

/* 当前使用的服务端地址（NVS 优先，其次 menuconfig 默认值），调试 / 屏幕显示用 */
const char *uploader_server_url(void);

/* WiFi 底层只初始化一次（netif / event loop / wifi_init）。配网模块也会调 */
esp_err_t uploader_wifi_init_once(void);

/* 配网时不希望 STA 自己乱连，置位后忽略 STA_START 的自动连接 */
void uploader_wifi_suppress_connect(bool on);

const char *uploader_code(void);   /* 6 位设备编号 */
bool uploader_has_token(void);
void uploader_forget_token(void);

bool uploader_wifi_connect(uint32_t timeout_ms);
void uploader_wifi_stop(void);
bool uploader_wifi_online(void);

/* 轮询配对，成功拿到 token 返回 true（内部带 3 秒一次的节流） */
bool uploader_pair(void);

up_result_t uploader_upload(const spool_item_t *item);

/* 让服务端立刻结束当前会话（生成转写与纪要） */
bool uploader_close_session(void);

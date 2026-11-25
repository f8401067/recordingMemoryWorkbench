/* 墙上时钟：给每段录音盖一个"真实录制时间"
 *
 * 为什么需要：设备出门随身带，录音先攒在本地，回家连上 WiFi 才补传。
 * 这时"到达服务器的时间"和"实际说话的日期"可能差一整天，服务端没法
 * 据此切分会话。所以每个分片要带上它自己录制的绝对时间。
 *
 * 两条时间来源，优先级从高到低：
 *   1. 板载 PCF85063 RTC（微雪 ePaper 板有，带纽扣电池，断电继续走）
 *   2. NVS 里上次校准的时间 + 本次开机已运行时长（没有 RTC 的板子用这个）
 *
 * 校准来源是任意一次 HTTP 响应的 Date 头（见 uploader.c），不依赖外网 NTP。
 * 拿不到校准值时 clock_valid() 为 false，上传就不带 recordedAt，
 * 服务端自动退回"按到达时间"的老逻辑，不会把数据搞坏。
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

/* 探测 RTC、读回上次校准值。失败也不影响录音，只是时间戳用不了 */
esp_err_t clock_init(void);

/* 时间是否可信（走过校准，且不是明显离谱的值） */
bool clock_valid(void);

/* 当前 UTC 时间（epoch 秒）。不可信时返回 0 */
uint32_t clock_now(void);

/* 用外部时间校准（HTTP Date 头 / 服务端时间）。会同时写 RTC 与 NVS。
 * 如果跳变明显，会顺手把待上传分片的录制时间整体平移过来 */
void clock_set(uint32_t epoch);

/* 把当前时间落盘一次。关机 / 深睡前调用，省得下次开机误差太大 */
void clock_persist(void);

/* 当前时间来源，调试和屏幕显示用："RTC" / "NVS" / "-" */
const char *clock_source(void);

/* UTC epoch → ISO8601（"2026-11-01T01:30:00Z"）。失败返回 false */
bool clock_iso8601(uint32_t epoch, char *out, size_t out_len);

/* 当前本地日期（"2026-11-03"），给屏幕显示用。时钟不可信时返回 false。
 * 只影响屏显，录音时间戳仍然是 UTC */
bool clock_local_date(char *out, size_t out_len);

/* 解析 HTTP Date 头（"Sun, 01 Nov 2026 01:30:00 GMT"）→ epoch，失败返回 0 */
uint32_t clock_parse_http_date(const char *s);

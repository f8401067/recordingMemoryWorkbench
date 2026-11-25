/* 本地录音暂存队列
 *
 * 录音先落盘，再决定什么时候上传：
 *   - 上传成功 → 立即删除，腾空间
 *   - 攒着（出门没网）→ 一直留着，回到已知 WiFi 或手动触发时补传
 *   - 整段没录到人声 → 直接丢，不浪费空间
 *
 * 存储介质：插了 TF 卡就用卡（容量大，出门一整天也存得下），
 * 没插 / 没配置引脚就退回片内 LittleFS（5MB，约 55 分钟纯语音）。
 * 挂载失败不影响录音，只会在日志里说明用的哪个介质。
 *
 * 文件名自带元信息：<seq>_<总时长ms>_<人声ms>_<录制起始epoch>.ogg
 * 最后一段是"这段录音是什么时候开始录的"，补传时服务端靠它还原真实时间线。
 * 老固件留下的三段式文件名照样能读，只是不带时间戳。
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#include "esp_err.h"

typedef struct {
    FILE *fp;
    char path[64];
    uint32_t seq;
    uint32_t bytes;
    uint32_t epoch; /* 本片开始录制的 UTC 时间（0 = 时钟不可用） */
} spool_rec_t;

typedef struct {
    char path[64];
    uint32_t seq;
    uint32_t dur_ms;
    uint32_t voiced_ms;
    uint32_t bytes;
    uint32_t epoch; /* 0 = 老文件，没有时间戳 */
} spool_item_t;

esp_err_t spool_init(void);

/* 当前用的是不是 TF 卡（false = 片内 Flash） */
bool spool_on_sd(void);

/* "SD" / "FLASH"，给屏幕和日志用 */
const char *spool_media(void);

/* 当前介质的可写总容量（字节） */
size_t spool_total_bytes(void);

/* 开一段新的暂存文件（写入中后缀是 .part） */
esp_err_t spool_begin(spool_rec_t *r);

/* 边录边写。返回 false 表示写失败（多半是空间满了） */
bool spool_write(spool_rec_t *r, const void *data, size_t len);

/* 把 .part 转成正式文件，文件名里带上时长 */
esp_err_t spool_commit(spool_rec_t *r, uint32_t dur_ms, uint32_t voiced_ms);

void spool_abort(spool_rec_t *r);

/* 列出待上传文件，按 seq 升序 */
int spool_list(spool_item_t *out, int max);

esp_err_t spool_remove(const spool_item_t *it);

/* 删除 seq 落在 [from, to] 区间的文件（用于"这段没录到人声，丢弃"） */
int spool_remove_range(uint32_t from_seq, uint32_t to_seq);

/* 把待上传分片的录制时间整体平移 delta_sec 秒（改文件名里的 epoch）。
 * 用于设备离线录音、联网对时后发现时钟跑偏的场景。返回实际改名的文件数。 */
int spool_shift_epochs(int32_t delta_sec);

size_t spool_free_bytes(void);
int spool_pending_count(void);

/* 开机时把掉电残留的 .part 收编成可上传文件 */
void spool_recover(void);

/* 保护 seq >= from_seq 的文件不被空间回收删掉（录音进行中调用，结束时传 0 解除） */
void spool_protect_from(uint32_t from_seq);

/* 当前正在录音的会话从哪个 seq 开始（0 = 当前没有录音会话）。
 * 上传任务用它判断"能不能收尾服务端会话"：还在录就别收尾。 */
uint32_t spool_protect_from_seq(void);

/* 上传任务正在读的文件：gc 期间跳过它，避免把上传中的文件删掉 */
void spool_mark_uploading(uint32_t seq);
/* 因为空间不足被丢掉的累计文件数（0 表示从没丢过） */
uint32_t spool_drop_count(void);

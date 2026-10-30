/* 本地录音暂存队列（LittleFS）
 *
 * 录音先落 Flash，再判断要不要上传：
 *   - 上传成功 → 立即删除，腾空间
 *   - 没网 / 服务器没开 → 留在原地，下次开机或定时唤醒再传
 *   - 整段没录到人声 → 直接丢，不浪费流量
 *
 * 文件名自带元信息：<seq>_<总时长ms>_<人声ms>.ogg
 * 这样不需要额外的索引文件，掉电也不会出现"文件在、元数据没了"。
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#include "esp_err.h"

typedef struct {
    FILE *fp;
    char path[48];
    uint32_t seq;
    uint32_t bytes;
} spool_rec_t;

typedef struct {
    char path[48];
    uint32_t seq;
    uint32_t dur_ms;
    uint32_t voiced_ms;
    uint32_t bytes;
} spool_item_t;

esp_err_t spool_init(void);

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

size_t spool_free_bytes(void);
int spool_pending_count(void);

/* 开机时把掉电残留的 .part 收编成可上传文件 */
void spool_recover(void);

/* 保护 seq >= from_seq 的文件不被空间回收删掉（录音进行中调用，结束时传 0 解除） */
void spool_protect_from(uint32_t from_seq);

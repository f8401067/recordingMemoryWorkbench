#include "spool.h"

#include <dirent.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <unistd.h>

#include "app_config.h"
#include "esp_littlefs.h"
#include "esp_log.h"
#include "nvs.h"

static const char *TAG = "spool";
static bool s_mounted;
static uint32_t s_next_seq = 1;
static uint32_t s_protect_from; /* 录音进行中的片段不许被回收 */

void spool_protect_from(uint32_t from_seq)
{
    s_protect_from = from_seq;
}

static uint32_t load_seq(void)
{
    nvs_handle_t h;
    uint32_t v = 1;
    if (nvs_open(APP_NVS_NS, NVS_READONLY, &h) == ESP_OK) {
        nvs_get_u32(h, "seq", &v);
        nvs_close(h);
    }
    return v ? v : 1;
}

static void store_seq(uint32_t v)
{
    nvs_handle_t h;
    if (nvs_open(APP_NVS_NS, NVS_READWRITE, &h) == ESP_OK) {
        nvs_set_u32(h, "seq", v);
        nvs_commit(h);
        nvs_close(h);
    }
}

esp_err_t spool_init(void)
{
    if (s_mounted) {
        return ESP_OK;
    }
    esp_vfs_littlefs_conf_t conf = {
        .base_path = APP_MOUNT_POINT,
        .partition_label = APP_PARTITION_LABEL,
        .format_if_mount_failed = true,
        .dont_mount = false,
    };
    esp_err_t err = esp_vfs_littlefs_register(&conf);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "mount %s failed: %s", APP_PARTITION_LABEL, esp_err_to_name(err));
        return err;
    }
    size_t total = 0, used = 0;
    esp_littlefs_info(APP_PARTITION_LABEL, &total, &used);
    ESP_LOGI(TAG, "mounted %s: %u/%u KB used", APP_MOUNT_POINT, (unsigned)(used / 1024),
             (unsigned)(total / 1024));
    s_mounted = true;
    s_next_seq = load_seq();
    spool_recover();
    return ESP_OK;
}

size_t spool_free_bytes(void)
{
    if (!s_mounted) {
        return 0;
    }
    size_t total = 0, used = 0;
    if (esp_littlefs_info(APP_PARTITION_LABEL, &total, &used) != ESP_OK) {
        return 0;
    }
    return total > used ? total - used : 0;
}

/* ------------------------------------------------------------------ *
 * 文件名解析 / 列表
 * ------------------------------------------------------------------ */
static bool parse_name(const char *name, spool_item_t *it)
{
    unsigned seq, dur, voiced;
    if (sscanf(name, "%6u_%u_%u.ogg", &seq, &dur, &voiced) != 3) {
        return false;
    }
    if (!it) {
        return true;
    }
    memset(it, 0, sizeof(*it));
    it->seq = seq;
    it->dur_ms = dur;
    it->voiced_ms = voiced;
    snprintf(it->path, sizeof(it->path), APP_MOUNT_POINT "/%.40s", name);
    struct stat st;
    if (stat(it->path, &st) == 0) {
        it->bytes = (uint32_t)st.st_size;
    }
    return true;
}

int spool_list(spool_item_t *out, int max)
{
    if (!s_mounted) {
        return 0;
    }
    DIR *d = opendir(APP_MOUNT_POINT);
    if (!d) {
        return 0;
    }
    int n = 0;
    struct dirent *e;
    while ((e = readdir(d)) != NULL) {
        if (n >= max) {
            break;
        }
        spool_item_t it;
        if (parse_name(e->d_name, &it)) {
            out[n++] = it;
        }
    }
    closedir(d);

    /* 按 seq 升序（文件不多，插入排序足够） */
    for (int i = 1; i < n; i++) {
        spool_item_t key = out[i];
        int j = i - 1;
        while (j >= 0 && out[j].seq > key.seq) {
            out[j + 1] = out[j];
            j--;
        }
        out[j + 1] = key;
    }
    return n;
}

int spool_pending_count(void)
{
    spool_item_t items[SPOOL_MAX_FILES];
    return spool_list(items, SPOOL_MAX_FILES);
}

esp_err_t spool_remove(const spool_item_t *it)
{
    if (!it) {
        return ESP_ERR_INVALID_ARG;
    }
    if (unlink(it->path) != 0 && errno != ENOENT) {
        ESP_LOGW(TAG, "unlink %s failed: %d", it->path, errno);
        return ESP_FAIL;
    }
    return ESP_OK;
}

int spool_remove_range(uint32_t from_seq, uint32_t to_seq)
{
    spool_item_t items[SPOOL_MAX_FILES];
    int n = spool_list(items, SPOOL_MAX_FILES);
    int removed = 0;
    for (int i = 0; i < n; i++) {
        if (items[i].seq >= from_seq && items[i].seq <= to_seq) {
            if (spool_remove(&items[i]) == ESP_OK) {
                removed++;
            }
        }
    }
    return removed;
}

/* ------------------------------------------------------------------ *
 * 空间回收：写之前先看看还剩多少
 * ------------------------------------------------------------------ */
static void gc_if_needed(void)
{
    size_t free_bytes = spool_free_bytes();
    if (free_bytes >= SPOOL_MIN_FREE_BYTES) {
        return;
    }
    spool_item_t items[SPOOL_MAX_FILES];
    int n = spool_list(items, SPOOL_MAX_FILES);
    /* 只删最老的一个：正常流程里文件传完就删，很少走到这 */
    for (int i = 0; i < n; i++) {
        if (s_protect_from && items[i].seq >= s_protect_from) {
            continue; /* 正在录的这一段不能删 */
        }
        ESP_LOGW(TAG, "空间不足（剩 %u KB），丢弃最老的 %s", (unsigned)(free_bytes / 1024),
                 items[i].path);
        spool_remove(&items[i]);
        return;
    }
    ESP_LOGE(TAG, "空间不足且没有可删的文件（都在保护区内）");
}

/* ------------------------------------------------------------------ *
 * 写入
 * ------------------------------------------------------------------ */
esp_err_t spool_begin(spool_rec_t *r)
{
    if (!s_mounted) {
        return ESP_ERR_INVALID_STATE;
    }
    gc_if_needed();

    memset(r, 0, sizeof(*r));
    r->seq = s_next_seq;
    snprintf(r->path, sizeof(r->path), APP_MOUNT_POINT "/%06u.part", (unsigned)r->seq);
    r->fp = fopen(r->path, "wb");
    if (!r->fp) {
        ESP_LOGE(TAG, "open %s failed", r->path);
        return ESP_FAIL;
    }
    return ESP_OK;
}

bool spool_write(spool_rec_t *r, const void *data, size_t len)
{
    if (!r->fp || len == 0) {
        return false;
    }
    size_t w = fwrite(data, 1, len, r->fp);
    if (w != len) {
        ESP_LOGE(TAG, "write failed (%u/%u)", (unsigned)w, (unsigned)len);
        return false;
    }
    r->bytes += (uint32_t)w;
    return true;
}

esp_err_t spool_commit(spool_rec_t *r, uint32_t dur_ms, uint32_t voiced_ms)
{
    if (!r->fp) {
        return ESP_ERR_INVALID_STATE;
    }
    fflush(r->fp);
    fclose(r->fp);
    r->fp = NULL;

    char final_path[48];
    snprintf(final_path, sizeof(final_path), APP_MOUNT_POINT "/%06u_%u_%u.ogg", (unsigned)r->seq,
             (unsigned)dur_ms, (unsigned)voiced_ms);
    if (rename(r->path, final_path) != 0) {
        ESP_LOGE(TAG, "rename %s -> %s failed", r->path, final_path);
        unlink(r->path);
        return ESP_FAIL;
    }

    s_next_seq = r->seq + 1;
    store_seq(s_next_seq);
    ESP_LOGI(TAG, "saved %s (%u bytes, %u ms, voiced %u ms)", final_path, (unsigned)r->bytes,
             (unsigned)dur_ms, (unsigned)voiced_ms);
    return ESP_OK;
}

void spool_abort(spool_rec_t *r)
{
    if (r->fp) {
        fclose(r->fp);
        r->fp = NULL;
    }
    if (r->path[0]) {
        unlink(r->path);
    }
}

/* ------------------------------------------------------------------ *
 * 掉电恢复
 * ------------------------------------------------------------------ */
void spool_recover(void)
{
    DIR *d = opendir(APP_MOUNT_POINT);
    if (!d) {
        return;
    }
    struct dirent *e;
    while ((e = readdir(d)) != NULL) {
        size_t len = strlen(e->d_name);
        if (len < 5 || strcmp(e->d_name + len - 5, ".part") != 0) {
            continue;
        }
        unsigned seq = 0;
        if (sscanf(e->d_name, "%6u.part", &seq) != 1) {
            continue;
        }
        char src[48], dst[48];
        snprintf(src, sizeof(src), APP_MOUNT_POINT "/%.40s", e->d_name);
        struct stat st;
        if (stat(src, &st) != 0) {
            continue;
        }
        if ((uint32_t)st.st_size < 1024) {
            /* 掉电时刚开个头，没有内容，直接扔 */
            unlink(src);
            continue;
        }
        /* 时长按 12kbps 估：bytes * 8 / bitrate * 1000 */
        uint32_t est_ms = (uint32_t)((uint64_t)st.st_size * 8 * 1000 / CONFIG_RECALL_OPUS_BITRATE);
        snprintf(dst, sizeof(dst), APP_MOUNT_POINT "/%06u_%u_%u.ogg", seq, (unsigned)est_ms,
                 (unsigned)est_ms);
        if (rename(src, dst) == 0) {
            ESP_LOGW(TAG, "recovered interrupted recording -> %s", dst);
            if (seq >= s_next_seq) {
                s_next_seq = seq + 1;
                store_seq(s_next_seq);
            }
        }
    }
    closedir(d);
}

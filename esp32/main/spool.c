#include "spool.h"

#include <dirent.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <unistd.h>

#include "app_config.h"
#include "clock.h"
#include "esp_littlefs.h"
#include "esp_log.h"
#include "nvs.h"

#if CONFIG_RECALL_SD_ENABLE
#include "driver/sdmmc_host.h"
#include "esp_vfs_fat.h"
#include "sdmmc_cmd.h"
#endif

static const char *TAG = "spool";

/* 当前介质：插卡成功就指向 SD，否则指向片内 LittleFS。
 * 下面所有路径都从 s_base 拼，不再直接写死 APP_MOUNT_POINT。 */
static const char *s_base = APP_MOUNT_POINT;
static bool s_on_sd;
static bool s_mounted;
static uint32_t s_next_seq = 1;
static uint32_t s_protect_from; /* 录音进行中的片段不许被回收 */
static volatile uint32_t s_uploading_seq; /* 上传任务正在读的文件，gc 也不能删 */
static volatile uint32_t s_drop_count;    /* gc 删过多少个文件（给界面告警用）*/

bool spool_on_sd(void)
{
    return s_on_sd;
}

const char *spool_media(void)
{
    return s_on_sd ? "SD" : "FLASH";
}

uint32_t spool_protect_from_seq(void)
{
    return s_protect_from;
}

void spool_mark_uploading(uint32_t seq)
{
    s_uploading_seq = seq;
}

uint32_t spool_drop_count(void)
{
    return s_drop_count;
}

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

/* ------------------------------------------------------------------ *
 * 挂载：先试 TF 卡，不行再退回片内 Flash
 *
 * 用户不一定会插卡，所以"没插"必须是一等公民：挂载失败只打一行日志，
 * 剩下的流程完全照旧，只是容量小一点。
 * ------------------------------------------------------------------ */
#if CONFIG_RECALL_SD_ENABLE
static sdmmc_card_t *s_card;

static bool mount_sd(void)
{
    if (PIN_SD_CLK < 0 || PIN_SD_CMD < 0 || PIN_SD_D0 < 0) {
        ESP_LOGW(TAG, "TF 卡引脚没配置（CLK/CMD/D0），继续用片内 Flash");
        return false;
    }

    /* 这块板的 TF 座是 **SDMMC 1-bit**（CLK/CMD/D0 三根线，没有 CS）：
     * 原理图上 SD_CS 是 NC，官方例程 04_SD_Card 也用 width=1 的 SDMMC 初始化。
     * 注意：不能用 sdspi，SPI 模式在这里没有 CS 可接。 */
    sdmmc_host_t host = SDMMC_HOST_DEFAULT();
    host.max_freq_khz = SDMMC_FREQ_HIGHSPEED;

    sdmmc_slot_config_t slot = SDMMC_SLOT_CONFIG_DEFAULT();
    slot.width = 1;
    slot.clk = PIN_SD_CLK;
    slot.cmd = PIN_SD_CMD;
    slot.d0 = PIN_SD_D0;

    esp_vfs_fat_sdmmc_mount_config_t mcfg = {
        .format_if_mount_failed = false, /* 别人的卡不能随便格式化 */
        .max_files = 6,
        .allocation_unit_size = 16 * 1024,
    };
    esp_err_t err = esp_vfs_fat_sdmmc_mount(APP_SD_MOUNT_POINT, &host, &slot, &mcfg, &s_card);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "没检测到可用的 TF 卡（%s），改用片内 Flash", esp_err_to_name(err));
        return false;
    }
    uint64_t mb = ((uint64_t)s_card->csd.capacity * s_card->csd.sector_size) / (1024 * 1024);
    ESP_LOGI(TAG, "TF 卡已挂载：%llu MB（SDMMC 1-bit CLK=%d CMD=%d D0=%d）",
             (unsigned long long)mb, PIN_SD_CLK, PIN_SD_CMD, PIN_SD_D0);
    return true;
}
#endif

esp_err_t spool_init(void)
{
    if (s_mounted) {
        return ESP_OK;
    }

#if CONFIG_RECALL_SD_ENABLE
    if (mount_sd()) {
        s_base = APP_SD_MOUNT_POINT;
        s_on_sd = true;
    }
#endif

    if (!s_on_sd) {
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
        s_base = APP_MOUNT_POINT;
    }

    ESP_LOGI(TAG, "存储介质 %s，挂载点 %s，可用 %u KB", spool_media(), s_base,
             (unsigned)(spool_free_bytes() / 1024));
    s_mounted = true;
    s_next_seq = load_seq();
    spool_recover();
    return ESP_OK;
}

size_t spool_free_bytes(void)
{
    if (s_on_sd) {
        /* FATFS 的 statvfs 在 IDF v6 里返回全 0（f_blocks/f_bavail 都没实现），
         * 真机第一次挂上卡就出现了“可用 0 KB”，而 gc 看到 0 会每 15 秒删一个
         * 最老文件——必须用 esp_vfs_fat_info() 拿真实数值 */
        uint64_t total = 0, free_b = 0;
        if (esp_vfs_fat_info(APP_SD_MOUNT_POINT, &total, &free_b) != ESP_OK) {
            return 0;
        }
        return (size_t)free_b;
    }
    size_t total = 0, used = 0;
    if (esp_littlefs_info(APP_PARTITION_LABEL, &total, &used) != ESP_OK) {
        return 0;
    }
    return total > used ? total - used : 0;
}

size_t spool_total_bytes(void)
{
    if (s_on_sd) {
        uint64_t total = 0, free_b = 0;
        if (esp_vfs_fat_info(APP_SD_MOUNT_POINT, &total, &free_b) != ESP_OK) {
            return 0;
        }
        return (size_t)total;
    }
    size_t total = 0, used = 0;
    if (esp_littlefs_info(APP_PARTITION_LABEL, &total, &used) != ESP_OK) {
        return 0;
    }
    return total;
}

/* ------------------------------------------------------------------ *
 * 文件名解析 / 列表
 *
 * <seq>_<dur>_<voiced>_<epoch>.ogg   新格式，带录制时间
 * <seq>_<dur>_<voiced>.ogg           老固件留下的，没有时间戳
 * ------------------------------------------------------------------ */
static bool parse_name(const char *name, spool_item_t *it)
{
    unsigned seq = 0, dur = 0, voiced = 0;
    unsigned long epoch = 0;
    int n = sscanf(name, "%6u_%u_%u_%lu.ogg", &seq, &dur, &voiced, &epoch);
    if (n < 3) {
        return false;
    }
    if (!it) {
        return true;
    }
    memset(it, 0, sizeof(*it));
    it->seq = seq;
    it->dur_ms = dur;
    it->voiced_ms = voiced;
    it->epoch = (n >= 4) ? (uint32_t)epoch : 0;
    snprintf(it->path, sizeof(it->path), "%s/%.48s", s_base, name);
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
    DIR *d = opendir(s_base);
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

int spool_shift_epochs(int32_t delta_sec)
{
    if (!s_mounted || delta_sec == 0) {
        return 0;
    }
    /* 先列出再改名：一边 readdir 一边 rename 会把同一个目录项再读一遍，
     * 有被重复平移的风险。放 .bss，别占上传任务的栈 */
    static spool_item_t items[SPOOL_MAX_FILES];
    int n = spool_list(items, SPOOL_MAX_FILES);
    int shifted = 0;
    for (int i = 0; i < n; i++) {
        if (items[i].epoch == 0) {
            continue; /* 老固件留下的文件没有时间戳，不动 */
        }
        int64_t ne = (int64_t)items[i].epoch + delta_sec;
        if (ne < 1000000000LL) {
            continue; /* 平移后不像个有效时间，跳过 */
        }
        char dst[96];
        snprintf(dst, sizeof(dst), "%s/%06u_%u_%u_%lu.ogg", s_base, (unsigned)items[i].seq,
                 (unsigned)items[i].dur_ms, (unsigned)items[i].voiced_ms, (unsigned long)ne);
        if (rename(items[i].path, dst) != 0) {
            ESP_LOGW(TAG, "平移时间戳失败：%s", items[i].path);
            continue;
        }
        shifted++;
    }
    return shifted;
}

int spool_pending_count(void)
{
    /* 只计数、不建数组：spool_item_t 占 88 字节，64 个就是 5.5KB，而主任务栈
     * 只有 CONFIG_ESP_MAIN_TASK_STACK_SIZE=3584。这里原来是
     * spool_item_t items[SPOOL_MAX_FILES]，一调用就把栈踩穿（READY 循环里
     * 每 20ms 一次）。改成流式计数，栈占用只剩几十字节。 */
    if (!s_mounted) {
        return 0;
    }
    DIR *d = opendir(s_base);
    if (!d) {
        return 0;
    }
    int n = 0;
    struct dirent *e;
    while ((e = readdir(d)) != NULL) {
        if (parse_name(e->d_name, NULL)) {
            n++;
        }
    }
    closedir(d);
    return n;
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
    /* 同样的道理：不要 64 元素的栈数组，直接遍历目录、命中就删 */
    if (!s_mounted) {
        return 0;
    }
    DIR *d = opendir(s_base);
    if (!d) {
        return 0;
    }
    int removed = 0;
    struct dirent *e;
    while ((e = readdir(d)) != NULL) {
        spool_item_t it;
        if (!parse_name(e->d_name, &it)) {
            continue;
        }
        if (it.seq < from_seq || it.seq > to_seq) {
            continue;
        }
        if (spool_remove(&it) == ESP_OK) {
            removed++;
        }
    }
    closedir(d);
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
    if (!s_mounted) {
        return;
    }
    /* 不再一次性列出全部文件（4KB 栈），改成扫一遍只记住最老的那个 */
    DIR *d = opendir(s_base);
    if (!d) {
        ESP_LOGE(TAG, "空间不足且目录打不开");
        return;
    }
    spool_item_t oldest = {0};
    bool found = false;
    struct dirent *e;
    while ((e = readdir(d)) != NULL) {
        spool_item_t it;
        if (!parse_name(e->d_name, &it)) {
            continue;
        }
        if (s_protect_from && it.seq >= s_protect_from) {
            continue; /* 正在录的这一段不能删 */
        }
        if (s_uploading_seq && it.seq == s_uploading_seq) {
            continue; /* 上传任务正读到一半 */
        }
        if (!found || it.seq < oldest.seq) {
            oldest = it;
            found = true;
        }
    }
    closedir(d);

    if (!found) {
        ESP_LOGE(TAG, "空间不足且没有可删的文件（都在保护区内）");
        return;
    }
    ESP_LOGW(TAG, "空间不足（剩 %u KB），丢弃最老的 %s", (unsigned)(free_bytes / 1024),
             oldest.path);
    if (spool_remove(&oldest) == ESP_OK) {
        s_drop_count++;
    }
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
    r->epoch = clock_now(); /* 这一片开始的绝对时间，补传时服务端靠它切会话 */
    snprintf(r->path, sizeof(r->path), "%s/%06u.part", s_base, (unsigned)r->seq);
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

    char final_path[64];
    snprintf(final_path, sizeof(final_path), "%s/%06u_%u_%u_%lu.ogg", s_base, (unsigned)r->seq,
             (unsigned)dur_ms, (unsigned)voiced_ms, (unsigned long)r->epoch);
    if (rename(r->path, final_path) != 0) {
        ESP_LOGE(TAG, "rename %s -> %s failed", r->path, final_path);
        unlink(r->path);
        return ESP_FAIL;
    }

    /* Ogg 封装是直接往 FILE* 里写的，spool_write 没参与，所以 r->bytes 一直是 0。
     * 真实大小只能封口后 stat 一次拿回来——日志长期报 "0 bytes" 会掩盖真正的
     * 写失败，上传链路（X-Chunk-Id 带大小）也拿不到真实值。 */
    struct stat st;
    if (stat(final_path, &st) == 0) {
        r->bytes = (uint32_t)st.st_size;
    }
    if (r->bytes == 0) {
        ESP_LOGE(TAG, "封口后文件是空的：%s（编码/写入链路有问题）", final_path);
        unlink(final_path);
        return ESP_FAIL;
    }

    s_next_seq = r->seq + 1;
    store_seq(s_next_seq);
    ESP_LOGI(TAG, "saved %s (%u bytes, %u ms, voiced %u ms, t=%lu)", final_path,
             (unsigned)r->bytes, (unsigned)dur_ms, (unsigned)voiced_ms, (unsigned long)r->epoch);
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
    DIR *d = opendir(s_base);
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
        char src[64], dst[64];
        snprintf(src, sizeof(src), "%s/%.48s", s_base, e->d_name);
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
        /* 没有可靠的文件创建时间可用，就按"现在"算。误差 = 关机时长，
         * 比完全没有时间戳强：至少能落到正确的那一天。 */
        uint32_t epoch = clock_now();
        snprintf(dst, sizeof(dst), "%s/%06u_%u_%u_%lu.ogg", s_base, seq, (unsigned)est_ms,
                 (unsigned)est_ms, (unsigned long)epoch);
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

#include "app.h"

#include <stdio.h>
#include <string.h>

#include "app_config.h"
#include "audio.h"
#include "display.h"
#include "esp_log.h"
#include "driver/gpio.h"
#include "esp_sleep.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "netcfg.h"
#include "soc/soc_caps.h"
#include "ogg_opus.h"
#include "spool.h"
#include "ui.h"
#include "uploader.h"
#include "vad.h"

static const char *TAG = "app";

typedef enum {
    ST_BOOT = 0,
    ST_PAIRING,
    ST_READY,
    ST_REC,
    ST_UPLOAD,
    ST_NOTICE,
} st_t;

static struct {
    st_t st;
    uint32_t st_enter;
    uint32_t last_user;
    uint32_t last_draw;
    uint32_t last_pair;
    uint32_t last_conn_try;

    /* 连点 3 下进配网 */
    uint8_t short_cnt;
    uint32_t short_at;
    uint8_t prov_rounds;

    /* 当前录音会话 */
    spool_rec_t sp;
    ogg_opus_rec_t enc;
    bool spool_open;
    uint32_t first_seq;
    uint32_t last_seq;
    uint32_t sess_ms;
    uint32_t sess_voiced_ms;
    uint32_t seg_ms;
    uint32_t seg_voiced_ms;

    vad_t vad;
    int16_t frame[APP_FRAME_SAMPLES];
    uint32_t last_voice;      /* 最后一次检测到人声的时刻，自动结束用 */

    /* 提示 */
    screen_kind_t notice_kind;
    char notice_line[40];
    char notice_hint[44];
    uint32_t notice_until;

    /* 上传进度 */
    int up_step;
    int up_total;
    bool blink;
} A;

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

static void go(st_t s)
{
    A.st = s;
    A.st_enter = now_ms();
}

static void notice(screen_kind_t kind, const char *line, const char *hint, uint32_t ms)
{
    A.notice_kind = kind;
    snprintf(A.notice_line, sizeof(A.notice_line), "%s", line ? line : "");
    snprintf(A.notice_hint, sizeof(A.notice_hint), "%s", hint ? hint : "");
    A.notice_until = now_ms() + ms;
    go(ST_NOTICE);
}

/* ------------------------------------------------------------------ *
 * 绘制
 * ------------------------------------------------------------------ */
static void draw(void)
{
    screen_model_t m = {0};
    snprintf(m.code, sizeof(m.code), "%s", uploader_code());
    m.blink = A.blink;

    switch (A.st) {
    case ST_BOOT:
        m.kind = SCR_BOOT;
        snprintf(m.title, sizeof(m.title), "BOOT");
        snprintf(m.line, sizeof(m.line), "RECALL MIC %s", APP_FW_VERSION);
        snprintf(m.hint, sizeof(m.hint), "STARTING");
        break;

    case ST_PAIRING:
        m.kind = SCR_PAIR;
        snprintf(m.title, sizeof(m.title), "PAIR");
        if (!netcfg_has_creds()) {
            snprintf(m.line, sizeof(m.line), "NO WIFI");
            snprintf(m.hint, sizeof(m.hint), "TAP 3X TO SETUP");
        } else if (!uploader_wifi_online()) {
            /* 凭据有了但还没连上：先把编号亮出来，连上就能直接去后台加 */
            snprintf(m.line, sizeof(m.line), "CONNECTING");
            snprintf(m.hint, sizeof(m.hint), "ADD IN WEB UI");
        } else {
            snprintf(m.line, sizeof(m.line), "DEVICE");
            snprintf(m.hint, sizeof(m.hint), "ADD IN WEB UI");
        }
        break;

    case ST_REC:
        m.kind = SCR_REC;
        snprintf(m.title, sizeof(m.title), "REC");
        m.secs = A.sess_ms / 1000;
        m.level = vad_level(&A.vad);
        snprintf(m.hint, sizeof(m.hint), "HOLD TO STOP / V%uS", (unsigned)(A.sess_voiced_ms / 1000));
        break;

    case ST_UPLOAD:
        m.kind = SCR_UPLOAD;
        snprintf(m.title, sizeof(m.title), "SEND");
        snprintf(m.line, sizeof(m.line), "UPLOAD");
        snprintf(m.hint, sizeof(m.hint), "KEEP POWER ON");
        m.step = A.up_step;
        m.steps = A.up_total;
        break;

    case ST_NOTICE:
        m.kind = A.notice_kind;
        snprintf(m.title, sizeof(m.title),
                 A.notice_kind == SCR_DONE ? "SENT" : (A.notice_kind == SCR_SKIP ? "SKIP" : "ERR"));
        snprintf(m.line, sizeof(m.line), "%s", A.notice_line);
        snprintf(m.hint, sizeof(m.hint), "%s", A.notice_hint);
        break;

    case ST_READY:
    default: {
        m.kind = SCR_READY;
        snprintf(m.title, sizeof(m.title), "READY");
        static uint32_t free_kb;
        static uint32_t free_at;
        if (now_ms() - free_at > 2000) {
            free_at = now_ms();
            free_kb = (uint32_t)(spool_free_bytes() / 1024);
        }
        /* 顶栏右边是设备编号，底下一行剩余空间，一行绑定状态 */
        snprintf(m.line, sizeof(m.line), "FREE %uKB", (unsigned)free_kb);
        snprintf(m.hint, sizeof(m.hint), "%s",
                 uploader_has_token() ? "PAIRED" : "UNPAIRED");
        break;
    }
    }

    display_render(&m);
}

/* ------------------------------------------------------------------ *
 * 录音会话
 * ------------------------------------------------------------------ */
static bool segment_begin(void)
{
    if (spool_begin(&A.sp) != ESP_OK) {
        return false;
    }
    if (ogg_opus_begin(&A.enc, A.sp.fp) != ESP_OK) {
        spool_abort(&A.sp);
        return false;
    }
    A.spool_open = true;
    if (A.first_seq == 0) {
        A.first_seq = A.sp.seq;
    }
    A.last_seq = A.sp.seq;
    spool_protect_from(A.first_seq); /* 录音期间不允许被空间回收删掉 */
    A.seg_ms = 0;
    A.seg_voiced_ms = 0;
    vad_reset_segment(&A.vad);
    return true;
}

static void segment_end(bool keep)
{
    if (!A.spool_open) {
        return;
    }
    ogg_opus_end(&A.enc);
    if (keep && A.sess_ms > 0) {
        /* 时长用编码进文件的真实长度：VAD 丢掉的静音不算时长，
         * 否则服务端会把 15 秒的文件记成 15 秒音频（实际只有几秒人声）*/
        uint32_t enc_ms = ogg_opus_ms(&A.enc);
        spool_commit(&A.sp, enc_ms, A.seg_voiced_ms);
    } else {
        spool_abort(&A.sp);
    }
    A.spool_open = false;
}

static void start_session(void)
{
    A.sess_ms = 0;
    A.sess_voiced_ms = 0;
    A.first_seq = 0;
    A.last_seq = 0;

    audio_start();
    /* 先丢弃一百多毫秒，等 I2S 稳定下来，否则开头会有"噗"的一声 */
    for (int i = 0; i < 8; i++) {
        audio_read(A.frame, APP_FRAME_SAMPLES, 100);
    }
    if (!segment_begin()) {
        audio_stop();
        notice(SCR_ERR, "FLASH FAIL", "FORMAT?", 3000);
        return;
    }
    ui_sound(SND_START);
    ui_led(LED_BLINK_SLOW);
    A.last_user = now_ms();
    A.last_voice = now_ms();
    A.st_enter = now_ms();
    go(ST_REC);
    ESP_LOGI(TAG, "开始录音 seq=%u", (unsigned)A.first_seq);
}

static void rotate_segment(void)
{
    /* 静音段不值得留：直接丢掉，重新开一个 */
    bool keep = A.seg_voiced_ms >= 800;
    segment_end(keep);
    if (!keep) {
        ESP_LOGD(TAG, "丢弃静音片段");
    }
    if (!segment_begin()) {
        audio_stop();
        notice(SCR_ERR, "FLASH FULL", "UPLOAD FIRST", 4000);
    }
}

static void finish_session(void)
{
    if (A.spool_open) {
        segment_end(A.seg_voiced_ms >= 800);
    }
    audio_stop();
    spool_protect_from(0);
    ui_led(LED_OFF);
    ui_sound(SND_STOP);

    ESP_LOGI(TAG, "结束录音：总 %u.%us，人声 %u.%us", (unsigned)(A.sess_ms / 1000),
             (unsigned)(A.sess_ms % 1000 / 100), (unsigned)(A.sess_voiced_ms / 1000),
             (unsigned)(A.sess_voiced_ms % 1000 / 100));

    if (A.sess_voiced_ms < SPOOL_MIN_VOICED_MS) {
        int n = spool_remove_range(A.first_seq, A.last_seq);
        char line[24];
        snprintf(line, sizeof(line), "V %u.%uS DROP %d",
                 (unsigned)(A.sess_voiced_ms / 1000), (unsigned)(A.sess_voiced_ms % 1000 / 100), n);
        notice(SCR_SKIP, "NO VOICE", "NOT UPLOADED", 2500);
        ESP_LOGI(TAG, "%s", line);
        return;
    }
    go(ST_UPLOAD);
}

/* ------------------------------------------------------------------ *
 * 上传
 * ------------------------------------------------------------------ */
static bool ensure_token(void)
{
    if (uploader_has_token()) {
        return true;
    }
    if (!uploader_wifi_connect(20000)) {
        return false;
    }
    for (int i = 0; i < 4; i++) {
        if (uploader_pair()) {
            ui_sound(SND_OK);
            return true;
        }
        vTaskDelay(pdMS_TO_TICKS(3000));
    }
    return uploader_has_token();
}

static void do_upload(void)
{
    spool_item_t items[SPOOL_MAX_FILES];
    int total = spool_list(items, SPOOL_MAX_FILES);
    A.up_total = total;
    A.up_step = 0;
    A.last_user = now_ms();
    draw();

    if (!uploader_wifi_connect(25000)) {
        ui_sound(SND_ERR);
        notice(SCR_ERR, "NO WIFI", "KEPT IN FLASH", 3500);
        return;
    }
    if (!ensure_token()) {
        ui_sound(SND_ERR);
        notice(SCR_ERR, "NOT PAIRED", "ADD IN WEB UI", 4000);
        return;
    }

    int sent = 0;
    uint32_t sent_bytes = 0;
    for (int i = 0; i < total; i++) {
        A.up_step = i + 1;
        draw();
        up_result_t r = uploader_upload(&items[i]);
        if (r == UP_OK) {
            sent_bytes += items[i].bytes;
            spool_remove(&items[i]);
            sent++;
            continue;
        }
        if (r == UP_TOKEN_INVALID) {
            uploader_forget_token();
            if (ensure_token()) {
                /* 重新配对成功，再来一次 */
                if (uploader_upload(&items[i]) == UP_OK) {
                    sent_bytes += items[i].bytes;
                    spool_remove(&items[i]);
                    sent++;
                    continue;
                }
            }
        }
        ESP_LOGW(TAG, "第 %d 个文件没传上去，留着下次再传", i + 1);
        break;
    }

    A.up_step = A.up_total;
    draw();

    if (spool_pending_count() == 0) {
#if CONFIG_RECALL_CLOSE_AFTER_UPLOAD
        uploader_close_session(); /* 立刻触发转写 + 纪要 */
#endif
        ui_sound(SND_OK);
        char line[24];
        snprintf(line, sizeof(line), "%d FILES %u.%uKB", sent, (unsigned)(sent_bytes / 1024),
                 (unsigned)(sent_bytes % 1024 / 100));
        uploader_wifi_stop();
        notice(SCR_DONE, line, "TRANSCRIBING", 3000);
    } else {
        ui_sound(SND_ERR);
        uploader_wifi_stop();
        notice(SCR_ERR, "SEND FAILED", "RETRY LATER", 3500);
    }
}

/* ------------------------------------------------------------------ *
 * WiFi 配网
 * ------------------------------------------------------------------ */
static void enter_portal(void)
{
    /* 配网前先停止可能正在跑的音频/上传，避免抢射频和 CPU */
    audio_stop();
    uploader_wifi_stop();

    screen_model_t m = {0};
    m.kind = SCR_PROV;
    snprintf(m.title, sizeof(m.title), "WIFI");
    snprintf(m.line, sizeof(m.line), "RECALL-%s", uploader_code());
    snprintf(m.hint, sizeof(m.hint), "192.168.4.1");
    display_render(&m);

    /* 配网期间主循环被 netcfg_portal 阻塞，闪烁没人驱动，所以用常亮表示"配置中" */
    ui_led(LED_ON);
    ui_sound(SND_START);
    ESP_LOGW(TAG, "进入配网：热点 Recall-%s，手机打开 http://192.168.4.1", uploader_code());

    bool saved = netcfg_portal((uint32_t)CONFIG_RECALL_PROV_TIMEOUT_S * 1000ULL);
    if (saved) {
        screen_model_t ok = {0};
        ok.kind = SCR_DONE;
        snprintf(ok.title, sizeof(ok.title), "OK");
        snprintf(ok.line, sizeof(ok.line), "WIFI SAVED");
        snprintf(ok.hint, sizeof(ok.hint), "REBOOTING");
        display_render(&ok);
        ui_sound(SND_OK);
        vTaskDelay(pdMS_TO_TICKS(600));
        esp_restart(); /* 重启后走正常开机流程，读取新凭据 */
    }

    ui_led(LED_OFF);
    ui_sound(SND_ERR);
    ESP_LOGW(TAG, "配网未完成");
    notice(SCR_ERR, "WIFI NOT SET", "TAP 3X RETRY", 2500);
}

/* ------------------------------------------------------------------ *
 * 深睡
 * ------------------------------------------------------------------ */
static void enter_deep_sleep(void)
{
    ESP_LOGI(TAG, "空闲 %d 秒，进入深睡（按一下按键唤醒）", CONFIG_RECALL_IDLE_SLEEP_S);
    display_power(false);
    audio_stop();
    uploader_wifi_stop();
    ui_led(LED_OFF);

    /* 按键唤醒：按下接地。S3 走 ext1（RTC IO），C3 走 GPIO 唤醒 */
#if SOC_PM_SUPPORT_EXT1_WAKEUP
    esp_sleep_enable_ext1_wakeup_io(1ULL << PIN_BTN, ESP_EXT1_WAKEUP_ANY_LOW);
#else
    gpio_wakeup_enable((gpio_num_t)PIN_BTN, GPIO_INTR_LOW_LEVEL);
    esp_sleep_enable_gpio_wakeup();
#endif
    /* 还有没传完的文件就定时醒一次重试 */
    if (spool_pending_count() > 0) {
        esp_sleep_enable_timer_wakeup(15ULL * 60 * 1000000ULL);
    }
    esp_deep_sleep_start();
}

/* ------------------------------------------------------------------ *
 * 主循环
 * ------------------------------------------------------------------ */
void app_run(void)
{
    memset(&A, 0, sizeof(A));
    vad_init(&A.vad, APP_FRAME_MS);

    uint32_t causes = esp_sleep_get_wakeup_causes();
    ESP_LOGI(TAG, "唤醒原因: 0x%x，待上传 %d 个文件", (unsigned)causes, spool_pending_count());
    if (causes & ((1u << ESP_SLEEP_WAKEUP_EXT1) | (1u << ESP_SLEEP_WAKEUP_GPIO))) {
        A.last_user = now_ms();
    }

    go(ST_BOOT);
    A.last_user = now_ms();

    uint32_t draw_at = now_ms();
    bool boot_upload_done = false;

    while (1) {
        uint32_t t = now_ms();
        A.blink = ((t / 400) & 1) != 0;
        ui_tick(t);
        ui_event_t ev = ui_poll(t);

        /* 屏幕 8Hz 刷新，够用又省电 */
        if (t - draw_at >= 125) {
            draw_at = t;
            draw();
        }

        /* 待命 / 配对状态下连点 3 下 → 重新配网（录音中不响应，防误触） */
        if ((A.st == ST_READY || A.st == ST_PAIRING) && ev == UI_EV_SHORT) {
            A.short_cnt = (t - A.short_at > 1500) ? 1 : (uint8_t)(A.short_cnt + 1);
            A.short_at = t;
            if (A.short_cnt >= 3) {
                A.short_cnt = 0;
                A.prov_rounds = 0;
                ESP_LOGI(TAG, "连点 3 下，进入配网");
                enter_portal();
                A.last_user = now_ms();
                continue;
            }
        }

        switch (A.st) {
        case ST_BOOT:
            /* 深睡被按键唤醒时用户还按着，这里直接接着算长按，省得再按一次 */
            if (ev == UI_EV_LONG) {
                start_session();
                break;
            }
            if (t - A.st_enter > 600) {
                go(uploader_has_token() ? ST_READY : ST_PAIRING);
            }
            vTaskDelay(pdMS_TO_TICKS(20));
            break;

        case ST_PAIRING:
            /* 没有 WiFi 凭据：开热点让手机来配，最多试 3 轮（约 9 分钟） */
            if (!netcfg_has_creds() && A.prov_rounds < 3) {
                A.prov_rounds++;
                enter_portal();
                break;
            }

            /* 没配对就一直开着 WiFi 等后台输入编号：未配对的设备本来也没法用 */
            if (!uploader_wifi_online() && (t - A.last_conn_try) > 10000) {
                A.last_conn_try = t;
                uploader_wifi_connect(15000);
            }
            if (t - A.last_pair > 3000) {
                A.last_pair = t;
                if (uploader_pair()) {
                    ui_sound(SND_OK);
                    ui_led(LED_OFF);
                    go(ST_READY);
                    break;
                }
            }
            if (ev == UI_EV_LONG) {
                start_session();
            }
            /* 一直没配网又没人管，先睡，按键唤醒会重新进配网 */
            if (!netcfg_has_creds() && (t - A.last_user) > 120000) {
                enter_deep_sleep();
            }
            vTaskDelay(pdMS_TO_TICKS(50));
            break;

        case ST_READY: {
            if (spool_pending_count() > 0 && !boot_upload_done) {
                boot_upload_done = true;
                go(ST_UPLOAD);
                break;
            }

#if CONFIG_RECALL_AUTO_SESSION
            /* 常驻监听：有人说话就自动开录 */
            audio_start();
            int n = audio_read(A.frame, APP_FRAME_SAMPLES, 100);
            if (n > 0 && vad_process(&A.vad, A.frame, n)) {
                ESP_LOGI(TAG, "VAD 触发自动录音");
                start_session();
                break;
            }
            if (ev == UI_EV_LONG) {
                start_session();
            }
#else
            if (ev == UI_EV_LONG) {
                start_session();
                break;
            }
            vTaskDelay(pdMS_TO_TICKS(20));
#endif

#if CONFIG_RECALL_IDLE_SLEEP_S > 0 && !CONFIG_RECALL_AUTO_SESSION
            /* 常驻监听模式下不深睡，否则 VAD 就没法自动开录了 */
            if (uploader_has_token() && spool_pending_count() == 0 &&
                (t - A.last_user) > (uint32_t)CONFIG_RECALL_IDLE_SLEEP_S * 1000) {
                enter_deep_sleep();
            }
#endif
            break;
        }

        case ST_REC: {
            int n = audio_read(A.frame, APP_FRAME_SAMPLES, 100);
            if (n <= 0) {
                break;
            }
            bool speech = vad_process(&A.vad, A.frame, n);
            A.sess_ms += APP_FRAME_MS;
            A.seg_ms += APP_FRAME_MS;
            if (speech) {
                ogg_opus_write(&A.enc, A.frame, n);
                A.sess_voiced_ms += APP_FRAME_MS;
                A.seg_voiced_ms += APP_FRAME_MS;
                A.last_voice = t;
            }
            if (A.seg_ms >= SPOOL_SEGMENT_MS) {
                rotate_segment();
            }
            if (ev == UI_EV_LONG) {
                finish_session();
                break;
            }
#if CONFIG_RECALL_AUTO_SESSION
            if (!A.vad.speech && (t - A.last_voice) > (uint32_t)CONFIG_RECALL_AUTO_STOP_MS) {
                ESP_LOGI(TAG, "静音超时，自动结束");
                finish_session();
            }
#endif
            break;
        }

        case ST_UPLOAD:
            do_upload();
            break;

        case ST_NOTICE:
            if (t > A.notice_until) {
                A.last_user = t;
                boot_upload_done = true;
                go(uploader_has_token() ? ST_READY : ST_PAIRING);
            }
            vTaskDelay(pdMS_TO_TICKS(20));
            break;
        }
    }
}

#include "app.h"

#include <stdio.h>
#include <string.h>

#include "app_config.h"
#include "audio.h"
#include "battery.h"
#include "clock.h"
#include "display.h"
#include "esp_log.h"
#include "driver/gpio.h"
#include "driver/usb_serial_jtag.h"
#include "esp_attr.h"
#include "esp_pm.h"
#include "esp_heap_caps.h"
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
    ST_MONITOR, /* 常驻监听：麦克风开着，等人声 */
    ST_PAUSED,  /* 短按 PWR 暂停：麦克风关掉，不检测人声也不录音 */
    ST_REC,
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

    bool draw_dirty; /* 只在状态切换时画屏（常驻模式省电） */
    int batt;        /* 电量 0~100，-1 = 读不到 */

    /* 只有双击上传时才把上传页画出来；监听/录音切换不动屏幕，
     * 但暂停/恢复会各刷一帧状态页（屏断电后画面保留）。
     * upload_ui_until 是结果页保留到什么时候，0 = 还在传；
     * upload_ui_start 是上传页亮起的时刻，一直没结果时靠它超时收场 */
    bool upload_ui;
    uint32_t upload_ui_until;
    uint32_t upload_ui_start;
    bool notice_power; /* 提示页需要把已经断电的墨水屏重新上电 */
    uint32_t screen_off_at; /* 静息画面刷完后延时断电，避免一直给屏供电 */
    int up_step_shown;      /* 上传页已经画出来的进度（节流用） */
    uint32_t up_draw_at;    /* 上次因为进度重画上传页的时刻 */
    bool wake_draw;         /* 这次画完需要把断电的屏重新点亮（监听时电量掉了） */
    int batt_shown;         /* 最近一次真正画到屏上的电量，-1 = 屏上没有电量 */

    /* 上传状态（后台任务写，主循环读来画界面） */
    uint32_t pending;
    bool sending;
    char up_note[24];
    uint32_t up_note_until;
    uint32_t drop_seen;
    uint32_t monitor_since; /* 进入监听的时刻：开头 1.5 秒不采信 VAD */
    bool storage_low;
    bool blink;

    /* 正在从"录音收尾"过渡到"暂停"：finish_session() 默认会回监听（重新开麦），
     * 暂停场景要拦住它，否则会白白多一轮开麦/关麦和一次屏幕刷新 */
    bool going_paused;
} A;

/* 本次启动是第几次：RTC 内存断电不保（断电重启会归零），软复位 / 看门狗 /
 * panic 重启都会保留。屏幕上的 "STARTING #N" 里 N>1 就说明正在重启循环里，
 * 不用接串口也能看出来。 */
RTC_NOINIT_ATTR static uint32_t s_boot_magic;
RTC_NOINIT_ATTR static uint32_t s_boot_cnt;
static uint32_t s_boot_count = 1;

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

/* ------------------------- 后台上传任务的状态 ------------------------- */
static TaskHandle_t s_up_task;
static volatile bool s_up_busy;          /* 正在传 */
static volatile int s_up_step;           /* 第几个 */
static volatile int s_up_total;          /* 一共几个 */
static volatile int s_up_result;         /* 0 无 / 1 成功 / -1 失败（主任务消费） */
static volatile uint32_t s_up_pending;   /* 待传文件数 */
static volatile bool s_up_close_wanted;  /* 合适的时候让服务端收尾（出转写） */
static volatile bool s_up_force;         /* 用户手动触发：不管攒了多少都传 */
static volatile bool s_up_sent_any;      /* 本次开机后成功传过至少一片 */
static int s_up_backoff_s;               /* 失败退避秒数 */
static int64_t s_last_pass_ms;           /* 上次成功上传轮次的时刻（定时补传用） */

/* 主循环调用：有活干了，叫醒上传任务。close_after_seq 是这段录音的最后一个分片 */
static void upload_request(bool close_when_done)
{
    if (close_when_done) {
        s_up_close_wanted = true;
    }
    if (s_up_task) {
        xTaskNotifyGive(s_up_task);
    }
}

/* 双击：用户想现在就把存着的传上去，不用等存满 */
static void upload_force(void)
{
    s_up_force = true;
    s_up_close_wanted = true; /* 传完顺手收尾，马上能看到文字 */
    if (s_up_task) {
        xTaskNotifyGive(s_up_task);
    }
}

/* 存储是不是快满了：满了就没法继续录，这时候必须联网腾地方。
 *
 * 阈值取"总容量的 20%"，上限 32MB（SD 卡再大也不用留几个 G 的余量），
 * 下限 512KB。片内 5MB → 剩 1MB 触发；32GB 卡 → 剩 32MB 触发。 */
static bool storage_pressure(void)
{
    size_t total = spool_total_bytes();
    if (total == 0) {
        return false;
    }
    size_t keep = total / 5;
    if (keep > 32u * 1024u * 1024u) {
        keep = 32u * 1024u * 1024u;
    }
    if (keep < 512u * 1024u) {
        keep = 512u * 1024u;
    }
    return spool_free_bytes() < keep;
}

static void go(st_t s)
{
    /* 监听/录音切换不刷屏：墨水屏全刷一次要两秒，还很费电，这两个状态由 LED 表示。
     * 暂停/恢复会在 toggle_pause 里各画一帧状态页，所以这里也不用重画。
     * 启动、配网、提示、待机这些页面仍然要画。 */
    if (s != ST_MONITOR && s != ST_REC && s != ST_PAUSED) {
        A.draw_dirty = true;
    }
    if (A.st != s) {
        ESP_LOGI(TAG, "状态 %d -> %d（%ums）", (int)A.st, (int)s,
                 (unsigned)(A.st ? now_ms() - A.st_enter : 0));
    }
    A.st = s;
    A.st_enter = now_ms();
}

/* 监听/录音/暂停是日常静默状态，不自动重画；
 * 只有双击上传页、启动/配网/提示/待机页才需要跟着数据更新。 */
static bool screen_auto(void)
{
    if (A.upload_ui && (A.st == ST_MONITOR || A.st == ST_PAUSED)) {
        return true;
    }
    return A.st != ST_REC && A.st != ST_MONITOR && A.st != ST_PAUSED;
}

static void notice(screen_kind_t kind, const char *line, const char *hint, uint32_t ms)
{
    A.notice_kind = kind;
    snprintf(A.notice_line, sizeof(A.notice_line), "%s", line ? line : "");
    snprintf(A.notice_hint, sizeof(A.notice_hint), "%s", hint ? hint : "");
    A.notice_until = now_ms() + ms;
    A.notice_power = true; /* 屏可能是断电的，画完要让主循环上电刷一次 */
    A.screen_off_at = 0;   /* 提示页显示期间不要把屏断掉 */
    go(ST_NOTICE);
}

/* ------------------------------------------------------------------ *
 * 绘制
 * ------------------------------------------------------------------ */
static void draw(void)
{
    screen_model_t m = {0};
    snprintf(m.code, sizeof(m.code), "%s", uploader_code());
    clock_local_date(m.date, sizeof(m.date)); /* 没校准过时留空，屏上不显示假日期 */
    m.blink = A.blink;
    m.batt = A.batt;
    A.batt_shown = A.batt; /* 记下这一帧画出去的电量，监听时靠它判断要不要补刷 */

    /* 双击上传期间只画上传页：WiFi 名称 + 进度，字号由屏根据剩余宽度选 */
    if (A.upload_ui && (A.st == ST_MONITOR || A.st == ST_PAUSED)) {
        m.kind = SCR_UPLOAD;
        const char *title = "SEND";
        if (!A.sending) {
            if (strcmp(A.up_note, "SENT") == 0) {
                title = "DONE";
            } else if (strcmp(A.up_note, "SEND FAIL") == 0) {
                title = "FAIL";
            }
        }
        snprintf(m.title, sizeof(m.title), "%s", title);
        if (!uploader_wifi_ssid(m.line, sizeof(m.line)) || !m.line[0]) {
            snprintf(m.line, sizeof(m.line), "NO WIFI");
        }
        if (A.sending) {
            snprintf(m.note, sizeof(m.note), "SENDING");
        } else {
            snprintf(m.note, sizeof(m.note), "%s", A.up_note[0] ? A.up_note : "READY");
        }
        m.step = s_up_step;
        m.steps = s_up_total > 0 ? s_up_total : (int)A.pending;
        if (m.step > m.steps) {
            m.step = m.steps;
        }
        snprintf(m.hint, sizeof(m.hint), "%s",
                 A.storage_low ? "STORAGE LOW" : "KEEP POWER ON");
        display_render(&m);
        return;
    }

    switch (A.st) {
    case ST_BOOT:
        m.kind = SCR_BOOT;
        snprintf(m.title, sizeof(m.title), "BOOT");
        snprintf(m.line, sizeof(m.line), "RECALL MIC %s", APP_FW_VERSION);
        if (s_boot_count > 1) {
            /* 多次启动 = 重启循环，把次数直接显示出来 */
            snprintf(m.hint, sizeof(m.hint), "STARTING #%u", (unsigned)s_boot_count);
        } else {
            snprintf(m.hint, sizeof(m.hint), "STARTING");
        }
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
            snprintf(m.hint, sizeof(m.hint), "WAIT FOR WIFI");
        } else {
            /* WiFi 通了：再告诉用户服务端到底通没通，别让人瞎等 */
            int srv = uploader_server_state();
            if (srv == 1) {
                snprintf(m.line, sizeof(m.line), "SERVER OK");
                /* 单账号服务器会自动认领，多账号才需要去后台点一下 */
                snprintf(m.hint, sizeof(m.hint), "AUTO PAIRING");
            } else if (srv == -2) {
                snprintf(m.line, sizeof(m.line), "SERVER ERR");
                snprintf(m.hint, sizeof(m.hint), "CHECK SERVER");
            } else if (srv == -1) {
                snprintf(m.line, sizeof(m.line), "NO SERVER");
                snprintf(m.hint, sizeof(m.hint), "CHECK URL / NET");
            } else {
                snprintf(m.line, sizeof(m.line), "SERVER ...");
                snprintf(m.hint, sizeof(m.hint), "CONTACTING");
            }
        }
        break;

    case ST_REC:
        m.kind = SCR_REC;
        snprintf(m.title, sizeof(m.title), "REC");
        m.secs = A.sess_ms / 1000;
        m.level = mvad_level(&A.vad);
        snprintf(m.line, sizeof(m.line), "DOUBLE TAP: STOP");
        if (A.pending) {
            snprintf(m.note, sizeof(m.note), "%u PENDING", (unsigned)A.pending);
        }
        snprintf(m.hint, sizeof(m.hint), "HOLD 3S: RESET WIFI");
        break;

    case ST_MONITOR:
        m.kind = SCR_LISTEN;
        snprintf(m.title, sizeof(m.title), "LISTEN");
        if (A.sending) {
            snprintf(m.line, sizeof(m.line), "SENDING %d/%d", s_up_step, s_up_total);
        } else if (A.up_note[0] && now_ms() < A.up_note_until) {
            snprintf(m.line, sizeof(m.line), "%s", A.up_note);
        } else if (A.pending) {
            snprintf(m.line, sizeof(m.line), "%u PENDING", (unsigned)A.pending);
        } else {
            snprintf(m.line, sizeof(m.line), "SPEAK ANYTIME");
        }
        snprintf(m.note, sizeof(m.note), "%s",
                 A.storage_low ? "STORAGE LOW: CLEANING" : "DBL: SEND");
        snprintf(m.hint, sizeof(m.hint), "HOLD 3S: RESET WIFI");
        break;

    case ST_PAUSED:
        m.kind = SCR_PAUSED;
        snprintf(m.title, sizeof(m.title), "PAUSE");
        snprintf(m.line, sizeof(m.line), "MIC + REC OFF");
        if (A.sending) {
            /* 暂停时也能双击上传：把进度/结果直接写在这一行，不用切回监听页 */
            snprintf(m.note, sizeof(m.note), "SENDING %d/%d", s_up_step, s_up_total);
        } else if (A.up_note[0] && now_ms() < A.up_note_until) {
            snprintf(m.note, sizeof(m.note), "%s", A.up_note);
        } else if (A.pending) {
            snprintf(m.note, sizeof(m.note), "%u PENDING", (unsigned)A.pending);
        }
        snprintf(m.hint, sizeof(m.hint), "PWR RESUME  DBL SEND");
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
        /* 顶栏右边是设备编号，底下一行剩余空间（带当前介质），一行绑定状态 */
        if (A.pending) {
            snprintf(m.line, sizeof(m.line), "%u PENDING", (unsigned)A.pending);
        } else {
            snprintf(m.line, sizeof(m.line), "%s %uKB", spool_media(), (unsigned)free_kb);
        }
        snprintf(m.hint, sizeof(m.hint), "%s",
                 uploader_has_token() ? "DBL: LISTEN" : "UNPAIRED");
        break;
    }
    }

    display_render(&m);
}

/* ------------------------------------------------------------------ *
 * 录音会话
 * ------------------------------------------------------------------ */
static bool s_mic_on;

/* 触发前的 300ms 不能丢：监听时一直留着最近 N 帧，开录时先补写进去。
 * 深度用 VAD_PREROLL_FRAMES 单独控制，和起判门槛 VAD_TRIGGER_FRAMES 解耦 ——
 * 调低起判门槛只是让 VAD 反应更快，不该连带缩短能补写回来的开头长度。 */
static int16_t s_pre[VAD_PREROLL_FRAMES][APP_FRAME_SAMPLES];
static int s_pre_idx;
static int s_pre_n;

/* 麦克风开/关。常驻监听时一直开着，所以 start_session 调它不会重复 open，
 * 也不会再丢 160ms 的开头 */
static void mic_start(void)
{
    if (s_mic_on) {
        return;
    }
    audio_start();
    /* 先丢弃一百多毫秒，等 I2S 稳定下来，否则开头会有"噗"的一声 */
    for (int i = 0; i < 8; i++) {
        audio_read(A.frame, APP_FRAME_SAMPLES, 100);
    }
    s_mic_on = true;
}

static void mic_stop(void)
{
    audio_stop();
    s_mic_on = false;
}

/* 暂停时：CPU 压到 80MHz 并允许自动 light sleep。
 * 麦克风已经彻底断电，I2S 不再占 PM 锁，空闲时整机能睡到 1mA 以下；
 * 恢复监听/录音前必须先调回来，否则 I2S 采样会被休眠打断。 */
static void pm_pause_mode(bool paused)
{
#if PIN_PWR_KEY != PIN_NC || PIN_BTN != PIN_NC
    if (paused) {
        /* 自动 light sleep 只会被定时器叫醒，按键全靠主循环 50ms 轮询去碰：
         * 快点点一下可能整段都在睡，表现就是"暂停后单击没反应、要按久一点才行"。
         * 把两个按键登记成 GPIO 唤醒源，按下立刻叫醒 CPU，
         * 主循环下一次采样就能拿到短按事件（gpio_wakeup_enable 会自动
         * 关掉该脚的 sleep isolate，CONFIG_PM_SLP_DISABLE_GPIO 也不会把它断开）。 */
#if PIN_PWR_KEY != PIN_NC
        gpio_wakeup_enable((gpio_num_t)PIN_PWR_KEY, GPIO_INTR_LOW_LEVEL);
#endif
#if PIN_BTN != PIN_NC
        gpio_wakeup_enable((gpio_num_t)PIN_BTN, GPIO_INTR_LOW_LEVEL);
#endif
        esp_sleep_enable_gpio_wakeup();
    }
#endif

    esp_pm_config_t pm = {
        .max_freq_mhz = paused ? 80 : 160,
        /* 空闲下限压到 40MHz（XTAL 频率）：常驻监听时每 20ms 只处理一帧 VAD，
         * 40MHz 足够，DFVS 有活会自动升到 160。比原来 80MHz 少几 mA */
        .min_freq_mhz = 40,
        .light_sleep_enable = paused,
    };
#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
    /* 插着 USB 调试时别进 light sleep：USB-Serial/JTAG 休眠会掉枚举，日志直接断 */
    if (paused && usb_serial_jtag_is_connected()) {
        pm.light_sleep_enable = false;
    }
#endif
    esp_err_t err = esp_pm_configure(&pm);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "电源模式切换失败：%s", esp_err_to_name(err));
    } else {
        /* 调 LED 亮度时用这行判断 light sleep 有没有真的开：
         * true 的话 LEDC 的 PWM 会在休眠期间停振，"微亮"可能变成时亮时灭 */
        ESP_LOGI(TAG, "电源模式：paused=%d, light_sleep=%d, usb=%d",
                 paused, pm.light_sleep_enable, usb_serial_jtag_is_connected());
#if CONFIG_PM_ENABLE
        /* 插着 USB 调试时把当前持有的 PM 锁打出来：暂停后如果这里还有
         * i2s_driver / sdmmc / spi_master 之类的 APB 锁，light sleep 其实进不去。
         * 正常情况暂停时应该只剩 esp_pm 自己的 rtos 锁（idle 时会释放）。 */
        if (paused && usb_serial_jtag_is_connected()) {
            esp_pm_dump_locks(stdout);
        }
#endif
    }
}

/* 双击上传：把上传页显示出来。暂停时屏和 CPU 都睡着了：先退出 light sleep
 * 并把页面画进后缓冲，再上电全刷一次，避免先刷旧画面再刷上传页（两次全刷）。 */
static void start_upload_ui(void)
{
    A.upload_ui = true;
    A.upload_ui_until = 0;
    A.upload_ui_start = now_ms();
    A.screen_off_at = 0;
    /* 上一轮留下的进度不能带过来，否则刚点完就显示 "5/5" */
    s_up_step = 0;
    s_up_total = 0;
    A.up_step_shown = 0;
    A.up_draw_at = now_ms();
    snprintf(A.up_note, sizeof(A.up_note), "SENDING");
    A.up_note_until = now_ms() + 3000;
    if (A.st == ST_PAUSED) {
        pm_pause_mode(false);
    }
    A.draw_dirty = false;
    draw();
    display_power(true);
}

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
    mvad_reset_segment(&A.vad);
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

static void start_session(bool use_preroll)
{
    A.sess_ms = 0;
    A.sess_voiced_ms = 0;
    A.first_seq = 0;
    A.last_seq = 0;

    mic_start();
    if (!segment_begin()) {
        mic_stop();
        notice(SCR_ERR, "FLASH FAIL", "FORMAT?", 3000);
        return;
    }
    /* 把触发判定期间（以及之前一点点）的音频补写进去，避免切掉开头 */
    if (use_preroll && s_pre_n > 0) {
        int start = (s_pre_idx + VAD_PREROLL_FRAMES - s_pre_n) % VAD_PREROLL_FRAMES;
        for (int i = 0; i < s_pre_n; i++) {
            ogg_opus_write(&A.enc, s_pre[(start + i) % VAD_PREROLL_FRAMES], APP_FRAME_SAMPLES);
        }
        uint32_t pre_ms = (uint32_t)s_pre_n * APP_FRAME_MS;
        A.sess_voiced_ms += pre_ms;
        A.seg_voiced_ms += pre_ms;
        ESP_LOGI(TAG, "补写触发前 %u ms", (unsigned)pre_ms);
    }
    s_pre_n = 0;
    s_pre_idx = 0;

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
        mic_stop();
        notice(SCR_ERR, "FLASH FULL", "UPLOAD FIRST", 4000);
    }
}

static void back_to_idle(void);

static void finish_session(void)
{
    if (A.spool_open) {
        segment_end(A.seg_voiced_ms >= 800);
    }
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
        ESP_LOGI(TAG, "%s", line);
        /* 误触发是常驻监听的常态，不值得每次刷屏；提示音和 LED 已经给了反馈 */
        if (!A.going_paused) {
            back_to_idle();
        }
        return;
    }

    /* 录音结束 → 文件交给后台任务传，主循环立刻回去继续听，不再"聋"几秒 */
    upload_request(true);
    back_to_idle();
}

/* ------------------------------------------------------------------ *
 * 上传
 * ------------------------------------------------------------------ */
/* 进入常驻监听：开麦、清 VAD、画一帧 LISTENING，之后不再刷屏 */
static void enter_monitor(void)
{
    /* 先恢复 160MHz / 关 light sleep：I2S 采样不能被休眠打断 */
    pm_pause_mode(false);
    /* 重新开一次麦克风：把上段录音期间积压在 DMA 里的旧音频丢掉 */
    mic_stop();
    mic_start();
    mvad_reset_segment(&A.vad);
    /* 刚开麦的几十~几百毫秒里，codec 上电会"砰"一下（宽频瞬态），
     * 很容易被 VAD 当成短促人声。这里记下时间，开头一段时间只更新噪声底、
     * 不触发录音。 */
    A.monitor_since = now_ms();
    A.last_voice = now_ms();
    A.last_user = now_ms();
    ui_led(LED_OFF); /* 监听时不闪灯，省电 */
    go(ST_MONITOR);
    /* 只在本次开机第一次进监听时画一帧静息页。之后监听/录音切换不动屏幕：
     * 全刷一次两秒，既闪又费电，状态看 LED 就够了。
     * 从暂停恢复的那一帧由 toggle_pause 负责画（屏上还留着 PAUSE）。 */
    static bool rest_drawn;
    if (!rest_drawn) {
        rest_drawn = true;
        A.draw_dirty = false;
        draw();
        display_power(true);
        /* 等这一帧真的刷到屏上再断电；墨水屏断电后画面还在，没必要一直供电 */
        A.screen_off_at = now_ms() + 3000;
    }
    ESP_LOGI(TAG, "进入常驻监听（静音 %u 秒后收尾上传）",
             (unsigned)(CONFIG_RECALL_SILENCE_SPLIT_MS / 1000));
}

/* 空闲去向：常驻模式进监听（要开麦），否则回待命 */
static void back_to_idle(void)
{
    /* 没有 WiFi 凭据（刚重置过 / 从没配过）：不管有没有 token 都先进配网，
     * 否则会停在 READY 上一直连不上网，用户看不出要做什么 */
    if (!netcfg_has_creds()) {
        go(ST_PAIRING);
        return;
    }
#if CONFIG_RECALL_ALWAYS_ON
    if (uploader_has_token() && !A.going_paused) {
        enter_monitor(); /* 麦继续开着，上传在后台跑 */
        return;
    }
#endif
    mic_stop();
    go(uploader_has_token() ? ST_READY : ST_PAIRING);
}

/* 短按 PWR：暂停人声检测 + 录音；再短按一下恢复监听。
 *
 * 暂停时把麦克风/编解码器整个关掉（不是只屏蔽 VAD 判断），这样绝不会偷偷录到
 * 用户不想录的内容；后台补传任务不受影响，照常传已封口的文件。
 * 正在录音时短按：先把当前这段正常收尾（会上传），再进暂停。 */
static void enter_paused(void)
{
    if (A.spool_open) {
        A.going_paused = true;
        finish_session(); /* 封口 + 收尾上传，不丢已经录的部分 */
        A.going_paused = false;
    }
    mic_stop();
    /* 没在传文件就把射频也关掉，别让它停在"连着但没网"的自动重连循环里 */
    if (!s_up_busy) {
        uploader_wifi_stop();
    }
    mvad_reset_segment(&A.vad);
    ui_led(LED_DIM); /* 暂停状态：绿 LED 微亮，表示"还开着但没在录" */
    A.last_user = now_ms();
    go(ST_PAUSED);
    /* 暂停页必须真的画到屏上：墨水屏断电后画面不消失，用户一眼能看出"现在没在录"。
     * 以前只靠 LED 表示，屏上还停在 LISTEN，按了跟没按一样。
     * 刷完立刻断电（画面留着），不用让屏一直供电；正在显示双击上传页时不动屏，
     * 交给结果页自己的停留/断电逻辑。 */
    A.draw_dirty = false;
    A.screen_off_at = 0;
    if (!A.upload_ui) {
        draw();
        display_power(true);  /* 屏可能已经断电，先上电把这帧刷出来 */
        display_power(false); /* 刷完立刻断电，画面保留 */
    }
    A.notice_power = false; /* 这一帧已经自己上电刷过了，别让主循环再上电一次把屏常亮 */
    /* 屏、麦克风、射频都关了，再压到 80MHz + 自动 light sleep。
     * 要是正好有文件在传，就别开休眠——等传完由上传任务自己补上 */
    if (!s_up_busy) {
        pm_pause_mode(true);
    }
    ui_sound(SND_STOP);
    ESP_LOGW(TAG, "短按 PWR：暂停监听/录音（再按一下恢复）");
}

static void toggle_pause(void)
{
    if (A.st == ST_PAUSED) {
        enter_monitor();
        /* 恢复后把监听页也刷出来：否则屏上一直留着 PAUSE（墨水屏断电后画面还在），
         * 用户没法确认已经恢复。刷完同样立刻断电，画面保留。 */
        if (!A.upload_ui) {
            A.draw_dirty = false;
            A.screen_off_at = 0;
            draw();
            display_power(true);
            display_power(false);
        }
        ui_sound(SND_START);
        ESP_LOGW(TAG, "短按 PWR：恢复监听");
    } else if (A.st == ST_MONITOR || A.st == ST_REC) {
        enter_paused();
    }
}

/* 长按 3 秒：只清 WiFi 列表，配对 token 保留。
 * 换网络（搬家 / 换路由器 / 换热点）不需要重新绑定，重启后自动进配网。 */
static void do_network_reset(void)
{
    ESP_LOGW(TAG, "长按重置键：清除 WiFi 列表（配对信息保留）");
    screen_model_t m = {0};
    m.kind = SCR_PAIR;
    snprintf(m.title, sizeof(m.title), "RESET");
    snprintf(m.line, sizeof(m.line), "WIFI CLEARED");
    snprintf(m.hint, sizeof(m.hint), "RESTARTING");

    display_render_blocking(&m);
    display_power(true); /* 屏可能是断电的，先把 WIFI CLEARED 刷出来 */

    mic_stop();
    uploader_wifi_stop();
    netcfg_clear_creds();
    ui_sound(SND_OK);
    vTaskDelay(pdMS_TO_TICKS(600));
    esp_restart();
}

/* ------------------------------------------------------------------ *
 * WiFi 配网
 * ------------------------------------------------------------------ */
static void enter_portal(void)
{
    /* 配网前先停止可能正在跑的音频/上传，避免抢射频和 CPU */
    mic_stop();
    uploader_pause(true);

    screen_model_t m = {0};
    m.kind = SCR_PROV;
    snprintf(m.title, sizeof(m.title), "WIFI");
    snprintf(m.line, sizeof(m.line), "RECALL-%s", uploader_code());
    snprintf(m.hint, sizeof(m.hint), "192.168.4.1");
    display_render_blocking(&m);

    /* 配网期间主循环被 netcfg_portal 阻塞，闪烁没人驱动，所以用常亮表示"配置中" */
    ui_led(LED_ON);
    ui_sound(SND_START);
    ESP_LOGW(TAG, "进入配网：热点 Recall-%s，手机打开 http://192.168.4.1", uploader_code());

    bool saved = netcfg_portal((uint32_t)CONFIG_RECALL_PROV_TIMEOUT_S * 1000ULL);
    if (saved) {
        bool factory = netcfg_factory_requested();
        screen_model_t ok = {0};
        ok.kind = SCR_DONE;
        if (factory) {
            /* 恢复出厂：WiFi 和 token 都已经在 netcfg 里清掉了，重启后
             * 会重新走"配网 + 自动认领"，不需要去服务端手动重置 */
            snprintf(ok.title, sizeof(ok.title), "RESET");
            snprintf(ok.line, sizeof(ok.line), "FACTORY CLEAN");
            ESP_LOGW(TAG, "已恢复出厂：WiFi 与配对信息全部清除");
        } else {
            snprintf(ok.title, sizeof(ok.title), "OK");
            snprintf(ok.line, sizeof(ok.line), "WIFI SAVED");
        }
        snprintf(ok.hint, sizeof(ok.hint), "REBOOTING");
        display_render_blocking(&ok);
        ui_sound(SND_OK);
        vTaskDelay(pdMS_TO_TICKS(600));
        esp_restart(); /* 重启后走正常开机流程，读取新凭据 */
    }

    uploader_pause(false);
    ui_led(LED_OFF);
    ui_sound(SND_ERR);
    ESP_LOGW(TAG, "配网未完成");
    notice(SCR_ERR, "WIFI NOT SET", "DOUBLE CLICK TO RETRY", 2500);
}

/* ------------------------------------------------------------------ *
 * 深睡
 * ------------------------------------------------------------------ */
static void enter_deep_sleep(void)
{
    ESP_LOGI(TAG, "空闲 %d 秒，进入深睡（按一下按键唤醒）", CONFIG_RECALL_IDLE_SLEEP_S);
    display_power(false);
    mic_stop();
    uploader_wifi_stop();
    ui_led(LED_OFF);

    /* 按键唤醒：按下接地。S3 走 ext1（RTC IO），C3 走 GPIO 唤醒 */
#if SOC_PM_SUPPORT_EXT1_WAKEUP
    esp_sleep_enable_ext1_wakeup_io(1ULL << PIN_BTN, ESP_EXT1_WAKEUP_ANY_LOW);
#else
    gpio_wakeup_enable((gpio_num_t)PIN_BTN, GPIO_INTR_LOW_LEVEL);
    esp_sleep_enable_gpio_wakeup();
#endif
    /* 还有没传完的文件就定时醒一次重试。只有"快存不下了"才值得醒，
     * 攒着没传的不用管——用户按键或回到 WiFi 时会补传 */
    if (spool_pending_count() > 0 && storage_pressure()) {
        esp_sleep_enable_timer_wakeup(15ULL * 60 * 1000000ULL);
    }
    clock_persist(); /* 把当前时间落到 NVS，下次开机误差小一点 */
    esp_deep_sleep_start();
}

/* 暂停超过 2 小时：屏/麦克风/射频在进暂停时就已经关了，这里只配唤醒源。
 * PWR 和 BOOT 都能叫醒，醒来是正常开机流程（系统重新进监听）。 */
static void pause_deep_sleep(void)
{
    ESP_LOGW(TAG, "暂停已超过 2 小时，进入深睡（按一下 PWR 唤醒）");
    display_power(false); /* 幂等：已经断过电就直接返回 */
    mic_stop();
    uploader_wifi_stop();
    ui_led(LED_OFF);

#if SOC_PM_SUPPORT_EXT1_WAKEUP
    uint64_t mask = 1ULL << PIN_BTN;
#if PIN_PWR_KEY != PIN_NC
    mask |= 1ULL << PIN_PWR_KEY;
#endif
    esp_sleep_enable_ext1_wakeup_io(mask, ESP_EXT1_WAKEUP_ANY_LOW);
#else
    gpio_wakeup_enable((gpio_num_t)PIN_BTN, GPIO_INTR_LOW_LEVEL);
#if PIN_PWR_KEY != PIN_NC
    gpio_wakeup_enable((gpio_num_t)PIN_PWR_KEY, GPIO_INTR_LOW_LEVEL);
#endif
    esp_sleep_enable_gpio_wakeup();
#endif
    clock_persist();
    esp_deep_sleep_start();
}

/* ------------------------------------------------------------------ *
 * 关机
 *
 * 这块板的 PWR 键不是硬开关：官方例程的做法是固件收到长按后把 VBAT_PWR
 * 拉低来断电。所以要在断电前把屏幕刷成大大的 OFF——墨水屏断电后画面还在，
 * 一眼就能分清"关着"还是"开着"。
 * ------------------------------------------------------------------ */
typedef enum {
    PWR_EV_NONE = 0,
    PWR_EV_SHORT, /* 短按：暂停 / 恢复监听 */
    PWR_EV_HOLD,  /* 按住 1.5 秒：关机 */
} pwr_ev_t;

/* PWR 键（GPIO18）是普通输入（按下接地），不是硬开关：
 *   短按（<1.5s）= 暂停/恢复监听录音
 *   按住 1.5 秒  = 关机（画 OFF 再拉低 VBAT_PWR）
 * 关机在按住第 1.5 秒时立刻触发，之后松手不会再补一个"短按"事件。 */
static pwr_ev_t pwr_key_poll(uint32_t now)
{
#if PIN_PWR_KEY != PIN_NC
    static bool down;
    static bool armed;     /* 见过一次松开才认按键，防止引脚一直接地导致开机就关机 */
    static bool hold_sent; /* 这次按住已经报过关机，松手别当成短按 */
    static uint32_t t0;
    bool d = gpio_get_level(PIN_PWR_KEY) == 0; /* 按下接地 */

    if (!d) {
        bool was_down = down;
        down = false;
        armed = true;
        if (was_down && !hold_sent) {
            uint32_t held = now - t0;
            if (held >= BTN_DEBOUNCE_MS && held < 1500) {
                return PWR_EV_SHORT;
            }
        }
        hold_sent = false;
        return PWR_EV_NONE;
    }
    if (!armed) {
        return PWR_EV_NONE;
    }
    if (!down) {
        down = true;
        t0 = now;
        hold_sent = false;
        return PWR_EV_NONE;
    }
    if (!hold_sent && (now - t0) >= 1500) {
        hold_sent = true;
        return PWR_EV_HOLD;
    }
    return PWR_EV_NONE;
#else
    (void)now;
    return PWR_EV_NONE;
#endif
}

static void power_off(void)
{
    ESP_LOGW(TAG, "长按 PWR：准备关机");

    /* 正在录的那段先封口，别留半截 .part */
    if (A.spool_open) {
        segment_end(A.seg_voiced_ms >= 800);
        spool_protect_from(0);
    }
    mic_stop();
    uploader_pause(true);
    uploader_wifi_stop();
    ui_led(LED_OFF);
    ui_sound(SND_STOP);

    /* 黑底大白字 OFF，并且等它真的刷到屏上再断电 */
    screen_model_t m = {0};
    m.kind = SCR_OFF;
    display_render_blocking(&m);
    display_power(true); /* 屏可能是断电的，先把 OFF 刷出来再关机 */
    vTaskDelay(pdMS_TO_TICKS(200));
    clock_persist();

#if PIN_VBAT_EN != PIN_NC
    gpio_set_level(PIN_VBAT_EN, 0); /* 断电池自锁 */
#endif
    vTaskDelay(pdMS_TO_TICKS(300));

    /* 插着 USB 时断不了电，就进深睡，按 PWR 再唤醒 */
#if PIN_PWR_KEY != PIN_NC && SOC_PM_SUPPORT_EXT1_WAKEUP
    esp_sleep_enable_ext1_wakeup_io(1ULL << PIN_PWR_KEY, ESP_EXT1_WAKEUP_ANY_LOW);
#endif
    esp_deep_sleep_start();
}

/* ------------------------------------------------------------------ *
 * 主循环
 * ------------------------------------------------------------------ */
/* ------------------------------------------------------------------ *
 * 后台上传任务（跑在 CPU1）
 *
 * 只做三件事：连 WiFi、配对/续期、把 spool 里已封口的文件传上去。
 * 主循环一行都不碰网络，所以上传时麦克风照常在听。
 * ------------------------------------------------------------------ */
static bool up_ensure_token(void)
{
    if (uploader_has_token()) {
        return true;
    }
    if (!uploader_wifi_connect(20000)) {
        return false;
    }
    for (int i = 0; i < 4; i++) {
        if (uploader_pair()) {
            return true;
        }
        vTaskDelay(pdMS_TO_TICKS(3000));
    }
    return uploader_has_token();
}

/* 传一轮，返回 true 表示 spool 已经清空 */
static bool up_one_pass(void)
{
    static spool_item_t items[SPOOL_MAX_FILES]; /* 4KB，放 .bss */

    /* 先只数个数：没东西可传就不开射频。真正的目录列表放到联网对时之后，
     * 因为离线录的分片 epoch 可能是旧时钟盖的，要先校时再平移 */
    int pending = spool_pending_count();
    s_up_total = pending;
    s_up_step = 0;

    /* 只有"该收尾"的时候也必须联网——常见情况是录音中途就把分片传完了，
     * 会话结束时 spool 已经是空的，不能因为"没文件可传"就跳过 */
    bool close_now = s_up_close_wanted && spool_protect_from_seq() == 0 && pending == 0;
    if (pending == 0 && !close_now) {
        return true;
    }

    if (!uploader_wifi_connect(25000)) {
        ESP_LOGW(TAG, "[upload] 连不上 WiFi（%d 个文件待传）", pending);
        return false;
    }
    /* uploader_wifi_connect 连上后会先用 Date 头对时（clock_set 顺手平移待传
     * 分片的 epoch），所以下面列目录拿到的已经是校正后的录制时间 */
    if (!up_ensure_token()) {
        ESP_LOGW(TAG, "[upload] 还没配对（%d 个文件待传）", pending);
        uploader_wifi_stop();
        return false;
    }

    int total = spool_list(items, SPOOL_MAX_FILES);
    s_up_total = total;
    s_up_step = 0;

    bool failed = false;
    for (int i = 0; i < total; i++) {
        s_up_step = i + 1;
        spool_mark_uploading(items[i].seq); /* 别让空间回收把它删了 */
        up_result_t r = uploader_upload(&items[i]);
        if (r == UP_TOKEN_INVALID) {
            uploader_forget_token();
            if (up_ensure_token() && uploader_upload(&items[i]) == UP_OK) {
                r = UP_OK;
            }
        }
        spool_mark_uploading(0);
        if (r != UP_OK) {
            ESP_LOGW(TAG, "[upload] seq=%u 没传上去，稍后重试", (unsigned)items[i].seq);
            failed = true;
            break;
        }
        spool_remove(&items[i]);
        s_up_sent_any = true;
    }

    /* 收尾：只在"确实没在录音"的时候做。否则会把一次连续录音切碎成好几条
     * 记录（服务端一收到 close 就出转写，后面的分片又开一条新的）。 */
    if (!failed && s_up_close_wanted && spool_protect_from_seq() == 0 &&
        spool_pending_count() == 0) {
        if (uploader_close_session()) {
            s_up_close_wanted = false;
        }
    }
    uploader_session_end(); /* 放掉复用的连接 */
    uploader_wifi_stop();   /* 传完立刻关射频 */
    return !failed;
}

static void upload_task(void *arg)
{
    (void)arg;
    for (;;) {
        /* 有唤醒就立刻干活；没有的话每分钟醒一次看看该不该传。
         * 注意"醒"不等于"联网"——真正的开关在下面那组条件里 */
        uint32_t wait_ms = s_up_backoff_s ? (uint32_t)s_up_backoff_s * 1000u : 60000u;
        if (wait_ms > 1800000u) {
            wait_ms = 1800000u;
        }
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(wait_ms));

        s_up_pending = (uint32_t)spool_pending_count();

        if (!uploader_has_token()) {
            /* 还没配对：定期试一次（配对成功后主循环会自己回到监听）。
             * 这一支不受"攒够 3 片"限制，否则没录音的时候永远配不上。 */
            s_up_busy = true;
            bool ok = up_ensure_token();
            s_up_busy = false;
            uploader_wifi_stop();
            if (ok) {
                s_up_result = 1;
            } else {
                s_up_result = (s_up_backoff_s == 0) ? -1 : 0;
                s_up_backoff_s = s_up_backoff_s ? (s_up_backoff_s < 1800 ? s_up_backoff_s * 2 : 1800) : 30;
            }
            continue;
        }

        {
            /* 什么时候才值得开射频：
             *   1. 用户双击手动触发（s_up_force）
             *   2. 存储快满了，再不腾地方就没法继续录
             *   3. 文件都传完了、只剩服务端收尾没发
             * 除此之外一律不联网——录音文件就躺在卡里，一整天的总耗电只有
             * 麦克风和 VAD，出门在外不想"有文件就传"把电池耗在 WiFi 上。 */
            bool recording = spool_protect_from_seq() != 0;
            bool tail_only = (s_up_pending == 0 && s_up_close_wanted && !recording &&
                              s_up_sent_any);
            /* 定时补传：距上次成功上传超过 RECALL_PERIODIC_UPLOAD_S 就主动传一次。
             * 只在有积压文件时才有意义——没文件时下面的早退分支会跳过，不开射频 */
            bool periodic = false;
#if CONFIG_RECALL_PERIODIC_UPLOAD_S > 0
            periodic = ((int64_t)now_ms() - s_last_pass_ms) >=
                       (int64_t)CONFIG_RECALL_PERIODIC_UPLOAD_S * 1000;
#endif

            if (s_up_pending == 0 && !tail_only) {
                s_up_backoff_s = 0;
                if (s_up_force) {
                    /* 双击上传但本地已经没有待传文件：也得回个结果，
                     * 否则上传页会一直停在 SENDING 上，直到兜底超时才收场 */
                    s_up_result = 1;
                }
                s_up_force = false;
                continue;
            }
            if (recording) {
                continue; /* 正在录音：等这段收尾再动射频，别抢 CPU 和电流 */
            }
            if (!s_up_force && !tail_only && !storage_pressure() && !periodic) {
                continue; /* 还没到该传的时候，文件继续躺着 */
            }
            if (periodic) {
                s_up_close_wanted = true; /* 定时传也顺手收尾，出转写和纪要 */
                ESP_LOGI(TAG, "[upload] 定时补传（间隔 %d 秒，%u 个文件待传）",
                         CONFIG_RECALL_PERIODIC_UPLOAD_S, (unsigned)s_up_pending);
            }
        }

        /* 暂停时如果有文件要传，先醒着传完，别让 light sleep 打断 WiFi/TLS。
         * 注意：暂停也可能发生在上传途中（enter_paused 看到 s_up_busy 就不切 PM），
         * 所以传完必须无条件按当前状态恢复一次，否则暂停后会一直停在高主频、
         * 不进 light sleep，看起来"暂停了还在耗电"。 */
        if (A.st == ST_PAUSED) {
            pm_pause_mode(false);
        }
        s_up_busy = true;
        bool ok = up_one_pass();
        s_up_busy = false;
        /* 兜底：up_one_pass 在"连不上 WiFi"等早退分支里走不到结尾的关射频，
         * 不补这一下射频会一直开着自动重连——这就是暂停时掉电快的主因 */
        uploader_wifi_stop();
        /* 传完还停在暂停（用户没恢复）：把低功耗模式补回去 */
        if (A.st == ST_PAUSED) {
            pm_pause_mode(true);
        }
        s_up_pending = (uint32_t)spool_pending_count();
        /* 只有第一次失败才提示音；持续失败只打日志，别每 30 秒响一下 */
        bool first_fail = (s_up_backoff_s == 0);
        s_up_result = ok ? 1 : (first_fail ? -1 : 0);

        if (ok) {
            s_up_backoff_s = 0;
            s_up_force = false;
            s_last_pass_ms = (int64_t)now_ms(); /* 记下这次成功上传，重新计 4 小时 */
        } else {
            if (s_up_backoff_s == 0) {
                s_up_backoff_s = 30;
            } else if (s_up_backoff_s < 1800) {
                s_up_backoff_s *= 2;
            }
#if CONFIG_RECALL_PERIODIC_UPLOAD_S > 0
            if (s_up_pending > 0 && !s_up_force && !storage_pressure()) {
                /* 这次多半是"到点定时补传"但当前没有可用 WiFi。把计时推到下一个
                 * 4 小时，否则退避会在 30 分钟一次地反复连网，出门一天白白耗电；
                 * 手动双击/存储将满触发的失败仍按退避重试 */
                s_last_pass_ms = (int64_t)now_ms();
            }
#endif
            ESP_LOGW(TAG, "[upload] 失败，%d 秒后重试（还剩 %u 个文件）", s_up_backoff_s,
                     (unsigned)s_up_pending);
        }
    }
}

void app_run(void)
{
    memset(&A, 0, sizeof(A));
    A.batt = -1;
    /* 开机先读一次电量：常驻状态只在开机画那一帧，等主循环的 10 秒定时器
     * 再读就来不及，第一帧会空着电量 */
    int pct0 = battery_percent();
    if (pct0 >= 0) {
        A.batt = pct0;
    }
    mvad_init(&A.vad, APP_FRAME_MS);

    /* 重启计数：用来判断"卡在 STARTING"到底是卡住了还是在反复重启 */
    if (s_boot_magic != 0x52454331u) { /* "REC1" */
        s_boot_magic = 0x52454331u;
        s_boot_cnt = 1;
    } else {
        s_boot_cnt++;
    }
    s_boot_count = s_boot_cnt;
    ESP_LOGW(TAG, "启动 #%u，复位原因 %d，堆余 %u 字节", (unsigned)s_boot_count,
             (int)esp_reset_reason(), (unsigned)esp_get_free_heap_size());

    uint32_t causes = esp_sleep_get_wakeup_causes();
    ESP_LOGI(TAG, "唤醒原因: 0x%x，待上传 %d 个文件", (unsigned)causes, spool_pending_count());
    if (causes & ((1u << ESP_SLEEP_WAKEUP_EXT1) | (1u << ESP_SLEEP_WAKEUP_GPIO))) {
        A.last_user = now_ms();
    }

    go(ST_BOOT);
    A.last_user = now_ms();

    /* 定时补传的计时起点：开机那一刻算起（开机本来就会补传一次） */
    s_last_pass_ms = (int64_t)now_ms();

    /* 上传任务固定跑 CPU1：TLS 握手很吃 CPU，别和 CPU0 上的录音抢时间片 */
    xTaskCreatePinnedToCore(upload_task, "upload", 12288, NULL, 3, &s_up_task, 1);

    uint32_t draw_at = now_ms();
    uint32_t batt_at = 0;
    uint32_t clock_at = 0;
#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
    /* USB 插拔会改变"暂停时能不能睡"（USB-Serial/JTAG 休眠会掉枚举）。
     * 之前只在进暂停那一刻判断一次，拔掉 USB 后会一直保持不睡，
     * 直到下一次状态切换——现在每秒复查一次，变了就重新评估。 */
    bool usb_prev = usb_serial_jtag_is_connected();
    uint32_t usb_at = 0;
#endif

    while (1) {
        uint32_t t = now_ms();
        A.blink = ((t / 400) & 1) != 0;
        ui_tick(t);
        ui_event_t ev = ui_poll(t);

        /* PWR 键：短按 = 暂停/恢复监听录音；按住 1.5 秒 = 关机（不返回） */
        {
            pwr_ev_t pev = pwr_key_poll(t);
            if (pev == PWR_EV_HOLD) {
                power_off();
            } else if (pev == PWR_EV_SHORT) {
                toggle_pause();
            }
        }

        /* 后台上传任务的状态：提示音只能在这里放（ui_* 不是线程安全的），
         * 界面也只在状态真的变了才重画，免得墨水屏一直闪 */
        {
            int res = s_up_result;
            if (res != 0) {
                s_up_result = 0;
                if (res > 0) {
                    ui_sound(SND_OK);
                    snprintf(A.up_note, sizeof(A.up_note), "SENT");
                } else {
                    ui_sound(SND_ERR);
                    snprintf(A.up_note, sizeof(A.up_note), "SEND FAIL");
                }
                A.up_note_until = t + 4000;
                if (A.upload_ui) {
                    A.upload_ui_until = t + 8000; /* 结果页停留 8 秒再断电 */
                    if (screen_auto()) {
                        A.draw_dirty = true;
                    }
                }
            }
            if (s_up_busy != A.sending) {
                A.sending = s_up_busy;
                if (screen_auto()) {
                    A.draw_dirty = true;
                }
            }
            /* 上传页进度：确实前进了才重画，而且 10 秒最多刷一次。
             * 墨水屏全刷一次要两秒，跟着每个文件刷反而费电 */
            if (A.upload_ui && s_up_busy && s_up_step != A.up_step_shown &&
                t - A.up_draw_at >= 10000) {
                A.up_step_shown = s_up_step;
                A.up_draw_at = t;
                if (screen_auto()) {
                    A.draw_dirty = true;
                }
            }
            if (!s_up_busy) {
                uint32_t pend = s_up_pending;
                if (pend != A.pending) {
                    A.pending = pend;
                    if (screen_auto()) {
                        A.draw_dirty = true;
                    }
                }
            }
            uint32_t drops = spool_drop_count();
            if (drops != A.drop_seen) {
                A.drop_seen = drops;
                A.storage_low = true;
                if (screen_auto()) {
                    A.draw_dirty = true;
                }
                ESP_LOGW(TAG, "空间不足，已经丢弃 %u 个最老的录音文件", (unsigned)drops);
            }
        }

        /* 上传页到点收场：结果页停 8 秒；如果一直没等到结果（没什么可传、
         * 后台静默失败），最多占 30 秒 —— 墨水屏断电后画面不会变，
         * 不收场就会一直停在上传界面。
         * 收场时把屏刷回当前状态页（监听 / 暂停），而不是只断电留旧画面。 */
        if (A.upload_ui) {
            bool ui_done = A.upload_ui_until
                               ? t >= A.upload_ui_until
                               : (!s_up_busy && t - A.upload_ui_start >= 30000);
            if (ui_done) {
                A.upload_ui = false;
                A.upload_ui_until = 0;
                A.upload_ui_start = 0;
                A.screen_off_at = 0;
                /* 先断电 → 新页面画进后缓冲 → 上电时一次全刷再断电：
                 * 上传页和监听页差别很大，直接局部刷新会留明显残影。
                 * 另外暂停时后台任务可能已经把 light sleep 切回来了，刷屏前
                 * 先退出休眠，否则 20ms 一次的等 BUSY 会让 GPIO 被隔离，
                 * 刷新被打断就剩半张图 */
                pm_pause_mode(false);
                display_power(false);
                A.draw_dirty = false;
                draw();
                display_power(true);
                display_power(false);
                if (A.st == ST_PAUSED && !s_up_busy) {
                    pm_pause_mode(true);
                }
            }
        }

        /* 静息画面刷完几秒后就把屏断电：画面留在屏上，但不再给面板供电 */
        if (A.screen_off_at && t >= A.screen_off_at) {
            A.screen_off_at = 0;
            if (!A.upload_ui && A.st != ST_NOTICE) {
                display_power(false);
            }
        }

        /* 电量：10 秒读一次。百分比变了才触发重画，录音中不画
         * （等这段录完回到监听时会带新电量重绘） */
        if (t - batt_at > 10000) {
            batt_at = t;
            int pct = battery_percent();
            if (pct >= 0 && pct != A.batt) {
                A.batt = pct;
                if (screen_auto()) {
                    A.draw_dirty = true;
                } else if (A.st == ST_MONITOR) {
                    /* 监听时屏是断电的、也不跟着状态重画，但电量每掉 10%
                     * 亮一次屏把数字刷上去（就几秒，一整天最多十次）。
                     * 跟"屏上现在画的电量"比，不是跟上一次读数比，
                     * 这样录完一段回到监听时发现差了一格也会补刷 */
                    int shown = A.batt_shown;
                    if (shown < 0 || pct / 10 < shown / 10) {
                        A.draw_dirty = true;
                        A.wake_draw = true;
                    }
                }
            }
        }

        /* 日期跨天：监听/录音时屏幕不按秒重画，过了零点要主动刷一次，
         * 否则顶栏的年月日会一直停在昨天。顺便覆盖"联网校准后才第一次
         * 拿到日期"的情况 */
        {
            static char shown[12];
            static uint32_t date_at;
            if (t - date_at >= 60u * 1000u) {
                date_at = t;
                char today[12];
                if (clock_local_date(today, sizeof(today)) && strcmp(today, shown) != 0) {
                    snprintf(shown, sizeof(shown), "%s", today);
                    if (screen_auto()) {
                        A.draw_dirty = true;
                    }
                }
            }
        }

        /* 时间：10 分钟落一次 NVS。原来只在关机/深睡时存，万一电池直接掉电，
         * 开机时钟会退回很久以前的值（误差 = 关机时长） */
        if (t - clock_at > 10u * 60u * 1000u) {
            clock_at = t;
            clock_persist();
        }

#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
        /* 插拔 USB 后重新评估电源模式（只在暂停时有影响） */
        if (t - usb_at >= 1000) {
            usb_at = t;
            bool usb_now = usb_serial_jtag_is_connected();
            if (usb_now != usb_prev) {
                usb_prev = usb_now;
                ESP_LOGI(TAG, "USB %s，重新评估电源模式", usb_now ? "已连接" : "已断开");
                /* 正在传文件就先别打断，传完上传任务会自己把 PM 补回来 */
                if (A.st == ST_PAUSED && !s_up_busy) {
                    pm_pause_mode(true);
                }
            }
        }
#endif

        /* 长按 3 秒：清配网（任何状态下都响应，不返回） */
        if (ev == UI_EV_RESET) {
            do_network_reset();
        }

        /* 屏幕刷新：平时 8Hz；常驻监听/录音时不刷（墨水屏刷一次很贵），
         * 只在状态切换时画一帧 */
#if CONFIG_RECALL_QUIET_SCREEN
        bool quiet_state = (A.st == ST_REC || A.st == ST_MONITOR || A.st == ST_PAUSED);
        if (A.draw_dirty || (!quiet_state && (t - draw_at >= 125))) {
#else
        if (t - draw_at >= 125) {
#endif
            draw_at = t;
            A.draw_dirty = false;
            /* 需要提示页 / 监听时电量掉了一格，屏可能是断电的：
             * 先把新画面画进后缓冲，再上电一次刷出来。
             * 顺序反了会先把旧画面刷一遍，多花两秒。 */
            bool notice_wake = A.notice_power;
            bool batt_wake = A.wake_draw;
            bool wake = notice_wake || batt_wake;
            A.notice_power = false;
            A.wake_draw = false;
            if (wake) {
                pm_pause_mode(false);
            }
            draw();
            if (wake) {
                display_power(true);
                /* 只为电量亮屏的，刷完停 3 秒就断电；提示页按自己的时长走 */
                if (batt_wake && !notice_wake) {
                    A.screen_off_at = t + 3000;
                }
            }
        }

        /* ---------------- 按键手势 ----------------
         * 双击      监听中 = 立刻上传；录音中 = 结束这段；待机 = 回监听
         * 长按 0.7s 停止录音回待机
         * 长按 3 秒  清 WiFi，重启后进配网（token 保留）
         * 双击（暂停中）立刻补传本地录音（麦克风保持关闭）
         * PWR 短按  暂停 / 恢复监听录音（暂停时麦克风彻底关闭）
         * PWR 按住 1.5s 关机
         */
        if (ev == UI_EV_DOUBLE) {
            if (A.st == ST_MONITOR) {
                ESP_LOGI(TAG, "双击：手动上传本地录音（%d 个待传）", spool_pending_count());
                /* 不切状态：切走就没人读麦克风了，监听会空两秒 */
                ui_sound(SND_START);
                start_upload_ui();
                upload_force();
                continue;
            }
            if (A.st == ST_REC) {
                ESP_LOGI(TAG, "双击结束这一段");
                finish_session(); /* 内部会回到监听 */
                continue;
            }
            if (A.st == ST_PAUSED) {
                ESP_LOGI(TAG, "双击（暂停中）：手动上传本地录音（%d 个待传）",
                         spool_pending_count());
                /* 不离开暂停状态：麦克风继续关着，只让后台上传任务跑 */
                ui_sound(SND_START);
                start_upload_ui();
                upload_force();
                continue;
            }
            if (A.st == ST_READY) {
                /* 待机里双击：有 token 就回监听，否则按老样子直接录一段 */
                if (uploader_has_token()) {
                    enter_monitor();
                } else {
                    start_session(false);
                }
                continue;
            }
        }

        switch (A.st) {
        case ST_BOOT:
            if (t - A.st_enter > 600) {
                if (spool_pending_count() > 0) {
                    /* 开机补传一次：多半是"出门攒了一天，回家重新开机"。
                     * 用 force 而不是悄悄传，是因为开机本来就耗电，
                     * 这一次传完能清净一整天 */
                    upload_force();
                } else if (!uploader_has_token()) {
                    /* 还没配对（刚恢复出厂 / 第一次上电）：立刻叫醒上传任务去认领，
                     * 别让它按 60 秒的节拍干等 */
                    upload_request(false);
                }
                back_to_idle();
            }
            vTaskDelay(pdMS_TO_TICKS(20));
            break;

        case ST_PAIRING:
            /* 没有 WiFi 凭据：开热点让手机来配，最多试 3 轮（约 9 分钟） */
            if (!netcfg_has_creds()) {
                if (A.prov_rounds < 3) {
                    A.prov_rounds++;
                    enter_portal();
                    break;
                }
                /* 试够了还是没配上：先睡，按键唤醒会重新进配网 */
                if ((t - A.last_user) > 120000) {
                    enter_deep_sleep();
                }
                vTaskDelay(pdMS_TO_TICKS(200));
                break;
            }

            /* 有凭据：联网和配对都交给后台任务做（它独占 WiFi），这里只看结果。
             * 以前主循环也去连 WiFi/配对，两个任务同时动 WiFi 会 abort。 */
            if (uploader_has_token()) {
                ui_sound(SND_OK);
                back_to_idle();
                break;
            }
            vTaskDelay(pdMS_TO_TICKS(50));
            break;

        case ST_READY: {
#if CONFIG_RECALL_AUTO_SESSION
            /* 常驻监听：有人说话就自动开录（双击手势在上面统一处理） */
            audio_start();
            int n = audio_read(A.frame, APP_FRAME_SAMPLES, 100);
            if (n > 0 && mvad_process(&A.vad, A.frame, n)) {
                ESP_LOGI(TAG, "VAD 触发自动录音");
                start_session(true);
                break;
            }
#else
            vTaskDelay(pdMS_TO_TICKS(20));
#endif

#if CONFIG_RECALL_IDLE_SLEEP_S > 0 && !CONFIG_RECALL_AUTO_SESSION && !CONFIG_RECALL_ALWAYS_ON
            /* 常驻监听模式下不深睡，否则 VAD 就没法自动开录了。
             * 攒着没传的文件不阻止深睡——真要存不下了 storage_pressure 会把它叫醒 */
            if (uploader_has_token() && (t - A.last_user) > (uint32_t)CONFIG_RECALL_IDLE_SLEEP_S * 1000) {
                enter_deep_sleep();
            }
#endif
            break;
        }

        case ST_MONITOR: {
            int n = audio_read(A.frame, APP_FRAME_SAMPLES, 100);
            if (n > 0) {
                memcpy(s_pre[s_pre_idx], A.frame, sizeof(A.frame));
                s_pre_idx = (s_pre_idx + 1) % VAD_PREROLL_FRAMES;
                if (s_pre_n < VAD_PREROLL_FRAMES) {
                    s_pre_n++;
                }
                bool speech = mvad_process(&A.vad, A.frame, n);
                uint32_t since = t - A.monitor_since;
                if (since < 1500) {
                    /* 开麦瞬态窗口：照常喂 VAD（噪声底要跟上），但不触发录音。
                     * 双击已经在上面的统一手势里处理掉了，这里不用再管 */
                    break;
                }
                if (speech) {
                    ESP_LOGI(TAG,
                             "检测到人声，开始录音（VAD rms=%.0f level=%d noise=%.0f gate=%.0f）",
                             (double)A.vad.last_rms, mvad_level(&A.vad),
                             (double)A.vad.noise_rms, (double)A.vad.last_gate);
                    start_session(true);
                    break;
                }
            }
            break;
        }

        case ST_PAUSED:
            /* 麦克风已关，没东西可读；给个 50ms 节拍省 CPU，等 PWR 键唤醒。
             * 暂停超过 2 小时自动深睡：这时屏、麦克风、射频都已经是关的，
             * 深睡只是把 CPU/PSRAM 也停掉，几乎不再掉电。插着 USB 调试时不睡。 */
            if (!s_up_busy && (t - A.st_enter) >= 2u * 60u * 60u * 1000u
#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
                && !usb_serial_jtag_is_connected()
#endif
            ) {
                pause_deep_sleep();
            }
            vTaskDelay(pdMS_TO_TICKS(50));
            break;

        case ST_REC: {
            int n = audio_read(A.frame, APP_FRAME_SAMPLES, 100);
            if (n <= 0) {
                break;
            }
            bool speech = mvad_process(&A.vad, A.frame, n);
            A.sess_ms += APP_FRAME_MS;
            A.seg_ms += APP_FRAME_MS;
            if (speech) {
                ogg_opus_write(&A.enc, A.frame, n);
                if (A.vad.last_above) {
                    /* 只统计"真的超阈值"的帧。拖尾那 600ms 静音不能算进有效人声，
                     * 否则敲一下桌子（100ms 瞬态 + 600ms 拖尾）就凑够 1 秒，
                     * 传上去一段没人说话的音频——之前服务端看到的就是这种。 */
                    A.sess_voiced_ms += APP_FRAME_MS;
                    A.seg_voiced_ms += APP_FRAME_MS;
                }
                A.last_voice = t;
            }
            if (A.seg_ms >= SPOOL_SEGMENT_MS) {
                rotate_segment();
            }
            /* 开录 3 秒还没攒到 500ms 人声：多半是撞击/摩擦误触发，立刻放弃，
             * 别让屏幕白挂 30 秒再判"没人说话" */
            if (A.sess_ms > 3000 && A.sess_voiced_ms < 500) {
                ESP_LOGW(TAG, "误触发（3 秒内人声只有 %u ms），放弃这段", (unsigned)A.sess_voiced_ms);
                finish_session();
                break;
            }
#if CONFIG_RECALL_ALWAYS_ON
            /* 常驻模式：安静够久就认为这段说完了，收尾并上传 */
            if (!A.vad.speech && (t - A.last_voice) > (uint32_t)CONFIG_RECALL_SILENCE_SPLIT_MS) {
                ESP_LOGI(TAG, "已静音 %u 秒，收尾并上传", (unsigned)(CONFIG_RECALL_SILENCE_SPLIT_MS / 1000));
                finish_session();
                break;
            }
#endif
#if CONFIG_RECALL_AUTO_SESSION
            if (!A.vad.speech && (t - A.last_voice) > (uint32_t)CONFIG_RECALL_AUTO_STOP_MS) {
                ESP_LOGI(TAG, "静音超时，自动结束");
                finish_session();
            }
#endif
            break;
        }

        case ST_NOTICE:
            if (t > A.notice_until) {
                A.last_user = t;
                back_to_idle();
                /* 回到日常状态后，提示画面再留一会儿就断电 */
                if (A.st == ST_MONITOR || A.st == ST_REC || A.st == ST_PAUSED) {
                    A.screen_off_at = t + 1500;
                }
            }
            vTaskDelay(pdMS_TO_TICKS(20));
            break;
        }
    }
}

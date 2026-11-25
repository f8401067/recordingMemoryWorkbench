/*
 * 忆录录音笔固件 · 全局配置
 *
 * 引脚按「S3 DevKitC-1」和「C3 SuperMini」两种最常见的最小系统板给，
 * 换板子只改这一段，其它模块不用动。
 */
#pragma once

#include "sdkconfig.h"
#include "driver/gpio.h"

/* ------------------------------------------------------------------ *
 * 音频
 * ------------------------------------------------------------------ */
#define APP_SAMPLE_RATE 16000
#define APP_FRAME_MS 20
#define APP_FRAME_SAMPLES (APP_SAMPLE_RATE / 1000 * APP_FRAME_MS) /* 320 */

/* INMP441 / ICS-43434 这类数字麦克风：24bit 数据左对齐在 32bit 帧里，
 * 右移 14 位既取到有效位又补了 +12dB 增益。声音偏小就调到 13，偏爆音就调到 15。 */
#define MIC_SHIFT 14
/* 麦克风数据位宽。INMP441 用 32；有些 PDM/I2S 麦只出 16bit，改成 16 */
#define MIC_SLOT_BITS 32

/* 直流阻断（一阶高通），INMP441 有几百 mV 直流偏置，不去掉 VAD 会一直判"有声" */
#define MIC_DC_ALPHA 0.995f

/* ------------------------------------------------------------------ *
 * VAD（人声检测）
 * 判据交给 ESP-SR 的 WebRTC VAD，这里只留"多久算一段、拖尾多长"
 * ------------------------------------------------------------------ */
/* VAD_MODE_0=Normal 1=Aggressive 2=VeryAggressive 3、4 更激进。
 * 越激进越不容易把噪声认成人声，但低信噪比下也越容易漏掉轻声说话。
 * 一开始用 2，实测偏严（说话开头、正常音量漏检），降到 1。
 * 环境安静想更保险可以到 0；嘈杂环境误触发多就回到 2。 */
#define VAD_SR_MODE 1

/* 连续多少帧判为人声才起判。8 帧 = 160ms：环境里的宽带噪声也很容易连成
 * 160ms，常驻监听就会出现"没人说话也录一段"。10 帧 = 200ms 是实测的安全
 * 边界：拿最近 67 条设备录音（33 条低价值 / 34 条有价值）做回归，11 帧以上
 * 会整条丢掉 19 秒的真对话（那条最长连续超阈值只有 10 帧），10 帧只少录
 * 2 条噪声片段。广播和旁人说话仍然会录——那是真的人声，靠时长门槛分不开。 */
#define VAD_TRIGGER_FRAMES 10

/* 触发前补写的音频长度，和起判门槛解耦：门槛降低不会让开头变短。
 * 15 帧 = 300ms，足够盖住"先说半个字 VAD 才反应"的延迟。 */
#define VAD_PREROLL_FRAMES 15

#define VAD_HANGOVER_MS 600    /* 判为静音后再录 600ms，避免切掉尾音 */

/* ESP-SR（WebRTC）VAD 真机上有个毛病：安静房间里 RMS≈15~50 的底噪也能被
 * 偶尔判成"人声"，而且会连续命中，结果没人说话也一直开录。所以再叠一道
 * 能量闸门：本帧必须同时满足「VAD 说是人声」且「RMS 明显高于噪声底」。
 *     gate = max(VAD_RMS_ABS_MIN, noise_rms * VAD_NOISE_FACTOR + VAD_NOISE_MARGIN)
 * 30dB 增益下正常说话 RMS 一般 500~3000，室内底噪 20~100，余量足够。
 * 如果发现轻声说话漏触发，把 VAD_RMS_ABS_MIN / VAD_NOISE_FACTOR 调小；
 * 嘈杂环境误触发多，就调大。350 实测会漏掉轻声说话，回退到 150。 */
#define VAD_RMS_ABS_MIN 150.0f  /* 绝对下限：能放过轻声口语，同时挡住大部分底噪 */
#define VAD_NOISE_FACTOR 2.5f   /* 必须比自适应噪声底高这么多倍 */
#define VAD_NOISE_MARGIN 50.0f  /* 再加固定余量，压住数字抖动 */

/* ------------------------------------------------------------------ *
 * 本地暂存
 * ------------------------------------------------------------------ */
#define SPOOL_SEGMENT_MS 15000        /* 每 15 秒落一个文件：断电最多丢 15 秒 */
#define SPOOL_MIN_FREE_BYTES (48 * 1024) /* 剩余空间低于这个值就删最老的 */
#define SPOOL_MAX_FILES 64

/* 上传判定：有效语音短于这个值就认为"这次没录到东西"，直接扔掉不上传 */
#define SPOOL_MIN_VOICED_MS CONFIG_RECALL_MIN_VOICED_MS

/* ------------------------------------------------------------------ *
 * 按键
 * ------------------------------------------------------------------ */
#define BTN_LONG_PRESS_MS 700   /* 按住超过 700ms 算一次"长按" */
#define BTN_CLICK_GAP_MS 450    /* 两下间隔小于这个算连击（双击要等这么久才确认） */
#define BTN_RESET_PRESS_MS 3000 /* 长按 3 秒 = 清配网（破坏性操作，留长一点防误触） */
#define BTN_DEBOUNCE_MS 30

/* ------------------------------------------------------------------ *
 * 引脚（用纯数字，方便在 #if 里判断"有没有接"）
 * ------------------------------------------------------------------ */
#define PIN_NC (-1)

#if CONFIG_IDF_TARGET_ESP32S3 && CONFIG_RECALL_BOARD_WS_EPAPER_154

/* ------------------------------------------------------------------ *
 * 微雪 ESP32-S3-ePaper-1.54（V1/V2 硬件引脚完全一致）
 *   1.54 寸电子墨水屏 200x200 单色（SPI，局部刷新）
 *   ES8311 一颗芯片管采集+播放，板上带功放和喇叭
 *   PCF85063 RTC / SHTC3 温湿度也挂在同一条 I2C 上
 * 引脚取自官方示例 02_Example/ESP-IDF/V2/08_Audio_Test：
 *   main/user_config.h + components/codec_board/board_cfg.txt（Board: S3_ePaper_1_54）
 * ------------------------------------------------------------------ */
#define BOARD_HAS_EXIO 0
#define BOARD_HAS_LCD_BL 0 /* 墨水屏不需要背光 */

/* 屏幕是电子墨水屏：刷新慢（局部约 0.4s，全刷约 2s），断电后画面保留 */
#define BOARD_DISPLAY_EPAPER 1

/* 三路电源使能。注意：EPD_PWR 和 AUDIO_PWR 是【低电平有效】，
 * 官方 board_power_bsp 里 POWEER_EPD_ON() 写的是 0。写反了屏和麦都不工作。 */
#define PIN_EPD_PWR 6   /* 墨水屏供电，低=开 */
#define PIN_AUDIO_PWR 42 /* ES8311 + 功放供电，低=开 */
#define PIN_SYS_POW PIN_NC /* 这板没有电池自锁脚 */
#define PIN_CODEC_PWR PIN_AUDIO_PWR
#define PIN_PA_EN 46    /* 功放使能，esp_codec_dev 自己管 */
#define BOARD_PWR_EN_ACTIVE_LOW 1

/* I2S：ES8311，必须给 MCLK（board_cfg 里 use_mclk: 1） */
#define PIN_MIC_MCLK 14
#define PIN_MIC_BCLK 15
#define PIN_MIC_WS 38
#define PIN_MIC_DIN 16  /* ES8311 ADC → 主控 */
#define PIN_MIC_DOUT 45 /* 主控 → ES8311 DAC */
#define PIN_MIC_PWR_EN PIN_NC

/* 主按键 BOOT(GPIO0)：双击=录音/结束或立即上传，长按 0.7s=回待机，
 * 长按 3 秒=清 WiFi 列表（重启后进配网，token 保留）。 */
#define PIN_BTN 0
#define PIN_BTN2 PIN_NC
/* PWR 键（GPIO18）：普通输入，按下接地（不是硬开关，读电平安全）。
 *   短按        = 暂停 / 恢复监听录音（暂停时麦克风彻底关掉）
 *   按住 1.5 秒 = 关机：先把 OFF 画到屏上，再拉低 VBAT_PWR 断电
 *                （官方例程 07_BATT_PWR_Test 的做法）。 */
#define PIN_PWR_KEY 18
#define PIN_LED 3      /* 板载 LED（官方 led_test 用的就是 GPIO3） */
#define PIN_BUZZER PIN_NC

#define PIN_I2C_SDA 47
#define PIN_I2C_SCL 48

/* 墨水屏 SPI：SPI2_HOST */
#define PIN_EPD_SCLK 12
#define PIN_EPD_MOSI 13
#define PIN_EPD_CS 11
#define PIN_EPD_DC 10
#define PIN_EPD_RST 9
#define PIN_EPD_BUSY 8

/* 电池供电自锁脚：拉高 = 用电池持续供电，拉低 = 断电。
 * 官方例程 07_BATT_PWR_Test 里叫 VBAT_PWR（board_div.VBAT_POWER_ON()），
 * 不拉高的话拔掉 USB 立刻断电、按 PWR 也只是闪一下就灭。 */
#define PIN_VBAT_EN 17

#define BOARD_LCD_W 200
#define BOARD_LCD_H 200

/* 板载绿 LED 是【低电平点亮】：原理图上 L2 双色 LED 的绿光阳极经 R3(24K)
 * 接 3V3，阴极接 GPIO3。之前这里写成 0（当成高电平点亮），导致 ui_led(LED_OFF)
 * 反而把灯点亮了——监听/暂停时灯一直亮着就是这个原因。 */
#define LED_ACTIVE_LOW 1

/* 麦克风走 ES8311 编解码芯片 */
#define BOARD_CODEC_ES8311 1
#define BOARD_ES8311_USE_MCLK 1

#elif CONFIG_IDF_TARGET_ESP32S3 && CONFIG_RECALL_BOARD_BOX0

/* ------------------------------------------------------------------ *
 * 正点原子 ATK-DNESP32S3 BOX0（小智 AI 语音机器人同款）
 *   1.54" ST7789 240x240 SPI 方屏，背光 GPIO42 直接驱动
 *   ES8311 一颗芯片同时管采集和播放（模拟咪头接它的 ADC）
 *   电容触摸走 I2C（CHSC5432 @0x2E，本固件暂不用）
 * 引脚取自小智固件 xiaozhi-esp32/boards/alientek/atk-dnesp32s3-box0/config.h
 * ------------------------------------------------------------------ */
#define BOARD_HAS_EXIO 0
#define BOARD_HAS_LCD_BL 1

/* 上电必须做三件事，否则设备会自己断电 / 麦克风没声 */
#define PIN_SYS_POW 2    /* 电源保持：拉高才不会松手断电 */
#define PIN_CODEC_PWR 14 /* ES8311 供电使能 */
#define PIN_PA_EN 21     /* 功放使能（放提示音时才开） */

/* I2S：ES8311，不用 MCLK（芯片从 BCLK 自己生成时钟，板载 LED 正好占着 GPIO13） */
#define PIN_MIC_BCLK 5
#define PIN_MIC_WS 10
#define PIN_MIC_DIN 6  /* ES8311 ADC → 主控 */
#define PIN_MIC_DOUT 9 /* 主控 → ES8311 DAC */
#define PIN_MIC_MCLK PIN_NC
#define PIN_MIC_PWR_EN PIN_NC

/* 三个实体按键：R(GPIO0)=BOOT，M(GPIO4)，L(GPIO3)。录音用 R 键 */
#define PIN_BTN 0
#define PIN_LED 13     /* 板载蓝灯 */
#define PIN_BUZZER PIN_NC

#define PIN_I2C_SDA 11
#define PIN_I2C_SCL 12

/* SPI 屏：SPI2_HOST */
#define PIN_TFT_SCLK 39
#define PIN_TFT_MOSI 40
#define PIN_TFT_MISO PIN_NC
#define PIN_TFT_CS 41
#define PIN_TFT_DC 38
#define PIN_TFT_RST PIN_NC
#define PIN_LCD_BL 42

/* 屏幕分辨率（横竖一样，是方屏） */
#define BOARD_LCD_W 240
#define BOARD_LCD_H 240

/* 板载蓝灯 GPIO13 高电平点亮 */
#define LED_ACTIVE_LOW 0

/* 麦克风走 ES8311 编解码芯片（否则是 INMP441 数字麦直采） */
#define BOARD_CODEC_ES8311 1

#elif CONFIG_IDF_TARGET_ESP32S3 && CONFIG_RECALL_BOARD_BOX3

/* ------------------------------------------------------------------ *
 * 正点原子 DNESP32S3 BOX3（ATK-DNESP32S3B3）
 *   2.4" ST7789V2 320x240 SPI 屏 + ES7210 采集 + ES8311 输出
 *   AW9523B 扩 16 个 IO：P1_0=LCD_BL P1_1=LED_RED P1_2=LED_BLUE
 *                        P0_0=KEY_K1 P0_1=KEY_K2
 * 引脚来自官网原理图，改板子请对照核对。
 * ------------------------------------------------------------------ */
#define BOARD_HAS_EXIO 1
#define BOARD_HAS_LCD_BL 0 /* 背光在 AW9523B P1_0 上，走 board_backlight() */
#define BOARD_CODEC_ES8311 0

/* I2S：ES7210(采集) / ES8311(输出) 共用一条 I2S 总线，MCLK 必须给 */
#define PIN_MIC_BCLK 38
#define PIN_MIC_WS 39
#define PIN_MIC_DOUT 40 /* 主控 → ES8311（播放用，录音不用） */
#define PIN_MIC_DIN 41  /* ES7210 → 主控 */
#define PIN_MIC_MCLK 21
#define PIN_MIC_PWR_EN PIN_NC

/* K0 直连 GPIO0（板载 BOOT 键，也是唯一的"物理"按键） */
#define PIN_BTN 0
#define PIN_LED PIN_NC    /* 蓝色灯挂在 AW9523B P1_2 上，走 board_led() */
#define PIN_BUZZER PIN_NC /* 没有蜂鸣器；提示音以后走 ES8311 */

#define PIN_I2C_SDA 3
#define PIN_I2C_SCL 2

/* SPI 屏：SPI2_HOST，和 TF 卡共用总线（TF 卡 CS 是 GPIO18） */
#define PIN_TFT_SCLK 15
#define PIN_TFT_MOSI 16
#define PIN_TFT_MISO 17
#define PIN_TFT_CS 47
#define PIN_TFT_DC 48
#define PIN_TFT_RST PIN_NC /* 屏幕复位由板级复位控制 */

/* AW9523B 上各功能的位（16 位，P0_x = bit x，P1_x = bit 8+x） */
#define EXIO_LCD_BL (1u << 8)  /* P1_0 */
#define EXIO_LED_RED (1u << 9) /* P1_1 */
#define EXIO_LED_BLUE (1u << 10) /* P1_2 */
#define EXIO_KEY_K1 (1u << 0)  /* P0_0 */
#define EXIO_KEY_K2 (1u << 1)  /* P0_1 */

#define BOARD_LCD_W 320
#define BOARD_LCD_H 240

#elif CONFIG_IDF_TARGET_ESP32S3

#define BOARD_HAS_EXIO 0
#define BOARD_HAS_LCD_BL 0
#define BOARD_CODEC_ES8311 0

#define PIN_SYS_POW PIN_NC
#define PIN_CODEC_PWR PIN_NC
#define PIN_PA_EN PIN_NC

#define PIN_MIC_BCLK 4
#define PIN_MIC_WS 5
#define PIN_MIC_DIN 6
#define PIN_MIC_DOUT PIN_NC
#define PIN_MIC_MCLK PIN_NC
#define PIN_MIC_PWR_EN PIN_NC /* 麦克风电源开关（可选，接 MOSFET 栅极）*/
#define PIN_BTN 14
#define PIN_LED 15
#define PIN_BUZZER 16
#define PIN_I2C_SDA 8
#define PIN_I2C_SCL 9
/* ST7735 160x80（可选） */
#define PIN_TFT_SCLK 12
#define PIN_TFT_MOSI 11
#define PIN_TFT_MISO PIN_NC
#define PIN_TFT_CS 10
#define PIN_TFT_DC 13
#define PIN_TFT_RST 21
#define PIN_LCD_BL PIN_NC

#define BOARD_LCD_W 320
#define BOARD_LCD_H 240

#elif CONFIG_IDF_TARGET_ESP32C3

#define BOARD_HAS_EXIO 0
#define BOARD_HAS_LCD_BL 0
#define BOARD_CODEC_ES8311 0

#define PIN_SYS_POW PIN_NC
#define PIN_CODEC_PWR PIN_NC
#define PIN_PA_EN PIN_NC

#define PIN_MIC_MCLK PIN_NC

#define PIN_MIC_BCLK 4
#define PIN_MIC_WS 5
#define PIN_MIC_DIN 6
#define PIN_MIC_PWR_EN PIN_NC
#define PIN_BTN 3
#define PIN_LED 10
#define PIN_BUZZER 7
#define PIN_I2C_SDA 0
#define PIN_I2C_SCL 1
/* C3 没有 TFT 选项，占位成 NC，让 lcd_panel.c 能编过 */
#define PIN_TFT_SCLK PIN_NC
#define PIN_TFT_MOSI PIN_NC
#define PIN_TFT_MISO PIN_NC
#define PIN_TFT_CS PIN_NC
#define PIN_TFT_DC PIN_NC
#define PIN_TFT_RST PIN_NC
#define PIN_LCD_BL PIN_NC

#define BOARD_LCD_W 320
#define BOARD_LCD_H 240

#else

#error "这个固件只针对 ESP32-S3 / ESP32-C3，请 idf.py set-target esp32s3 或 esp32c3"

#endif

/* 板载 LED 常见是低电平点亮；BOX0 的蓝灯是高电平点亮（在各分支里覆盖） */
#ifndef LED_ACTIVE_LOW
#define LED_ACTIVE_LOW 1
#endif

/* 暂停时板载灯的"微亮"亮度（0~100%）。0 = 全灭；太大就不是"微亮"了。
 * 只有普通 GPIO 驱动的灯能调光（IO 扩展上的灯只能开/关）。
 * 40% 对一颗 20mA 的灯就是 8mA 常亮，暂停一久比监听还费；降到 8% 还能看清。 */
#ifndef LED_DIM_PERCENT
#define LED_DIM_PERCENT 8
#endif

/* 屏幕是不是电子墨水屏（刷新慢、断电保留画面） */
#ifndef BOARD_DISPLAY_EPAPER
#define BOARD_DISPLAY_EPAPER 0
#endif

/* 电源使能脚是高电平开还是低电平开（微雪那块板是低电平开） */
#ifndef BOARD_PWR_EN_ACTIVE_LOW
#define BOARD_PWR_EN_ACTIVE_LOW 0
#endif

/* ES8311 要不要主控给 MCLK（BOX0 不给，微雪那块必须给） */
#ifndef BOARD_ES8311_USE_MCLK
#define BOARD_ES8311_USE_MCLK 0
#endif

/* 墨水屏引脚占位，让其它板型也能编过 */
#ifndef PIN_EPD_PWR
#define PIN_EPD_PWR PIN_NC
#define PIN_EPD_SCLK PIN_NC
#define PIN_EPD_MOSI PIN_NC
#define PIN_EPD_CS PIN_NC
#define PIN_EPD_DC PIN_NC
#define PIN_EPD_RST PIN_NC
#define PIN_EPD_BUSY PIN_NC
#endif

#ifndef PIN_VBAT_EN
#define PIN_VBAT_EN PIN_NC
#endif

/* 第二按键（重置配网）。没有这块板子的就当作没接 */
#ifndef PIN_BTN2
#define PIN_BTN2 PIN_NC
#endif

/* 电源键（关机用）。没有这块板子的就当作没接 */
#ifndef PIN_PWR_KEY
#define PIN_PWR_KEY PIN_NC
#endif

/* ------------------------------------------------------------------ *
 * TF 卡（可选）
 *
 * 走 **SDMMC 1-bit**（CLK / CMD / D0），不是 SPI——这块板子的 TF 座
 * 直接把 SD 的 CMD/D0 接出来，DAT3(CS) 是 NC。
 * 微雪 ESP32-S3-Touch-ePaper-1.54 原理图（2025-11 版）实测：
 *   CLK=GPIO39  CMD=GPIO41  D0=GPIO40
 * 官方例程 02_Example/ESP-IDF/V2/04_SD_Card 也是这么写的：
 *   SDMMC_D0=40, SDMMC_CLK=39, SDMMC_CMD=41, width=1
 *
 * 只有 menuconfig 里打开「用 TF 卡存录音」并填好引脚才会启用；
 * 关闭、没插卡、卡不是 FAT32，都会自动退回片内 LittleFS，
 * 录音链路完全不受影响。
 * ------------------------------------------------------------------ */
#if CONFIG_RECALL_SD_ENABLE
#define PIN_SD_CLK CONFIG_RECALL_SD_CLK
#define PIN_SD_CMD CONFIG_RECALL_SD_CMD
#define PIN_SD_D0 CONFIG_RECALL_SD_D0
#else
#define PIN_SD_CLK PIN_NC
#define PIN_SD_CMD PIN_NC
#define PIN_SD_D0 PIN_NC
#endif
#define APP_SD_MOUNT_POINT "/sdcard"

/* ------------------------------------------------------------------ *
 * 杂项
 * ------------------------------------------------------------------ */
#define APP_FW_VERSION "1.0.0"
#define APP_PARTITION_LABEL "spool"
#define APP_MOUNT_POINT "/spool"

#define APP_NVS_NS "recall"

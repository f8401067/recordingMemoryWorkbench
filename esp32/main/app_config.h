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
 * 判据：短时 RMS 超过「自适应噪声底 × 倍数」且过零率不像纯噪声
 * ------------------------------------------------------------------ */
#define VAD_TRIGGER_RATIO 2.6f  /* 高于噪声底多少倍算人声 */
#define VAD_MIN_RMS 150.0f      /* 绝对下限，安静房间别被风扇骗 */
#define VAD_TRIGGER_FRAMES 2    /* 连续 2 帧超阈值才起（40ms），防咔哒声 */
#define VAD_HANGOVER_MS 600     /* 低于阈值后继续录 600ms，避免切掉尾音 */
#define VAD_NOISE_ALPHA 0.04f   /* 噪声底跟随速度 */
#define VAD_MAX_ZCR 0.42f       /* 过零率上限，滤掉键盘/摩擦这类高频噪声 */

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
#define BTN_DEBOUNCE_MS 30

/* ------------------------------------------------------------------ *
 * 引脚（用纯数字，方便在 #if 里判断"有没有接"）
 * ------------------------------------------------------------------ */
#define PIN_NC (-1)

#if CONFIG_IDF_TARGET_ESP32S3 && CONFIG_RECALL_BOARD_BOX0

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

/* ------------------------------------------------------------------ *
 * 杂项
 * ------------------------------------------------------------------ */
#define APP_FW_VERSION "1.0.0"
#define APP_PARTITION_LABEL "spool"
#define APP_MOUNT_POINT "/spool"

#define APP_NVS_NS "recall"

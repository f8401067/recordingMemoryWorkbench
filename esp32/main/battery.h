/* 电池电量：读板上锂电池电压
 *
 * 硬件：微雪 ESP32-S3-ePaper-1.54 把电池正极经过 1:2 分压接到
 * ADC1 通道 3（GPIO4）。参数与换算取自官方例程
 * 02_Example/ESP-IDF/V2/01_ADC_Test/components/adc_bsp/adc_bsp.c
 * 和 11_FactoryProgram 的 port_adc.cpp（满 4.12V / 空 3.0V 线性映射）。
 *
 * 只插 USB、没接电池时读数会在 4.5V 以上或接近 0，百分比不可信，
 * 这里用 -1 表示"读不到"。
 */
#pragma once

#include "esp_err.h"

esp_err_t battery_init(void);

/* 电池电压（V）。读失败返回 0 */
float battery_voltage(void);

/* 0~100；-1 表示读不到（没接电池 / ADC 没起来） */
int battery_percent(void);

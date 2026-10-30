/* 板级支持：I2C 总线、AW9523B IO 扩展（背光 / LED）、板载资源开关
 *
 * 谁用 I2C 都从这里拿总线句柄，避免同一个 I2C 端口被初始化两次
 * （ESP-IDF 重复 i2c_new_master_bus 会直接报错，早期版本还会不停重启）。
 */
#pragma once

#include <stdbool.h>

#include "driver/i2c_master.h"
#include "esp_err.h"

/* 必须在 display_init() / audio_init() 之前调用 */
esp_err_t board_init(void);

/* 全板共用的 I2C 主总线（没初始化成功时返回 NULL） */
i2c_master_bus_handle_t board_i2c_bus(void);

/* 屏幕背光。没有 IO 扩展的板子上是空操作 */
void board_backlight(bool on);

/* 板载状态灯。BOX3 上是 AW9523B 的 P1_2（蓝色），普通板子是 PIN_LED */
void board_led(bool on);

/* AW9523B 的原始读写，调试用；没有扩展芯片时返回 ESP_ERR_NOT_SUPPORTED */
esp_err_t board_exio_write(uint8_t reg, uint8_t val);
esp_err_t board_exio_read(uint8_t reg, uint8_t *val);

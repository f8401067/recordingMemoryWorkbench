/* Captive Portal 的 DNS 劫持：把所有域名查询都回答成热点自己的 IP
 *
 * 手机连上 WiFi 后会悄悄访问一个探测地址来判断"这个网能不能上网"：
 *   iOS      captive.apple.com/hotspot-detect.html
 *   Android  connectivitycheck.gstatic.com/generate_204
 *   Windows  www.msftncsi.com/ncsi.txt
 * 只要 DNS 把这些域名都解析到 192.168.4.1，请求就会打到我们的 httpd 上，
 * 再由 httpd 的 404 handler 统一 303 重定向到配网首页，系统就弹出登录窗口。
 *
 * 实现照抄 ESP-IDF 官方示例 protocols/http_server/captive_portal 的 dns_server，
 * 去掉了"按规则匹配"的部分：配网场景就是无差别劫持。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

/* 启动 DNS 劫持服务器。if_key 一般用 "WIFI_AP_DEF"，
 * 服务器会直接拿这个网卡的 IP 作为所有查询的答案。 */
bool dns_redirect_start(const char *if_key);

/* 停止并释放。配网结束后必须调，否则会占着一个 socket 和 4KB 栈的任务 */
void dns_redirect_stop(void);

bool dns_redirect_running(void);

/* AP 起来后把热点 IP 告诉劫持服务器（网络字节序） */
void dns_redirect_set_ip(uint32_t ip_network_order);

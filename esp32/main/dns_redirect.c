/* Captive Portal · DNS 劫持
 *
 * 源码来自 ESP-IDF 官方示例 examples/protocols/http_server/captive_portal
 * （SPDX-License-Identifier: Unlicense OR CC0-1.0），
 * 这里按本项目的需要做了一点简化：只支持"全部劫持"，不再按域名匹配。
 */
#include "dns_redirect.h"

#include <errno.h>
#include <string.h>
#include <sys/param.h>

#include "esp_check.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lwip/err.h"
#include "lwip/netdb.h"
#include "lwip/sockets.h"
#include "lwip/sys.h"

static const char *TAG = "dnsredir";

#define DNS_PORT (53)
#define DNS_MAX_LEN (256)

#define OPCODE_MASK (0x7800)
#define QR_FLAG (1 << 7)
#define QD_TYPE_A (0x0001)
#define ANS_TTL_SEC (300)

typedef struct __attribute__((__packed__)) {
    uint16_t id;
    uint16_t flags;
    uint16_t qd_count;
    uint16_t an_count;
    uint16_t ns_count;
    uint16_t ar_count;
} dns_header_t;

typedef struct {
    uint16_t type;
    uint16_t class;
} dns_question_t;

typedef struct __attribute__((__packed__)) {
    uint16_t ptr_offset;
    uint16_t type;
    uint16_t class;
    uint32_t ttl;
    uint16_t addr_len;
    uint32_t ip_addr;
} dns_answer_t;

static TaskHandle_t s_task;
static volatile bool s_running;
static int s_sock = -1;
static uint32_t s_ap_ip; /* 热点自己的 IP，网络字节序 */

/* 把 DNS 报文里的域名（长度前缀的 label 序列）还原成 "a.b.c" 形式，
 * 返回指向问题区（type/class）的指针 */
static char *parse_dns_name(char *raw_name, char *parsed_name, size_t max_len)
{
    char *label = raw_name;
    char *itr = parsed_name;
    int name_len = 0;

    do {
        int sub_len = *label;
        name_len += (sub_len + 1);
        if (name_len > (int)max_len) {
            return NULL;
        }
        memcpy(itr, label + 1, sub_len);
        itr[sub_len] = '.';
        itr += (sub_len + 1);
        label += sub_len + 1;
    } while (*label != 0);

    parsed_name[name_len - 1] = '\0';
    return label + 1;
}

/* 解析查询并就地拼出应答 */
static int parse_dns_request(char *req, size_t req_len, char *reply, size_t reply_max_len)
{
    if (req_len > reply_max_len) {
        return -1;
    }
    memset(reply, 0, reply_max_len);
    memcpy(reply, req, req_len);

    dns_header_t *header = (dns_header_t *)reply;

    /* 不是标准查询（比如动态更新）就不回 */
    if ((header->flags & OPCODE_MASK) != 0) {
        return 0;
    }
    /* 置 QR 位：这是应答不是查询 */
    header->flags |= QR_FLAG;

    uint16_t qd_count = ntohs(header->qd_count);
    header->an_count = htons(qd_count);

    int reply_len = qd_count * (int)sizeof(dns_answer_t) + (int)req_len;
    if (reply_len > (int)reply_max_len) {
        return -1;
    }

    char *cur_ans_ptr = reply + req_len;
    char *cur_qd_ptr = reply + sizeof(dns_header_t);
    char name[128];

    for (int i = 0; i < qd_count; i++) {
        char *name_end = parse_dns_name(cur_qd_ptr, name, sizeof(name));
        if (name_end == NULL) {
            return -1;
        }
        dns_question_t *q = (dns_question_t *)name_end;
        uint16_t qd_type = ntohs(q->type);
        uint16_t qd_class = ntohs(q->class);

        /* 只回 A 记录（IPv4）。AAAA 查询不回，让手机退回 IPv4 */
        if (qd_type != QD_TYPE_A) {
            continue;
        }

        ESP_LOGD(TAG, "劫持 %s → 热点", name);
        dns_answer_t *ans = (dns_answer_t *)cur_ans_ptr;
        /* 0xC000 = 名字压缩指针，指回问题区的域名 */
        ans->ptr_offset = htons(0xC000 | (uint16_t)(cur_qd_ptr - reply));
        ans->type = htons(qd_type);
        ans->class = htons(qd_class);
        ans->ttl = htonl(ANS_TTL_SEC);
        ans->addr_len = htons(sizeof(s_ap_ip));
        ans->ip_addr = s_ap_ip;

        cur_ans_ptr += sizeof(dns_answer_t);
        cur_qd_ptr = name_end + sizeof(dns_question_t);
    }
    return reply_len;
}

static void dns_task(void *arg)
{
    (void)arg;
    char rx[DNS_MAX_LEN];
    char reply[DNS_MAX_LEN];

    struct sockaddr_in dest = {0};
    dest.sin_addr.s_addr = htonl(INADDR_ANY);
    dest.sin_family = AF_INET;
    dest.sin_port = htons(DNS_PORT);

    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
    if (sock < 0) {
        ESP_LOGE(TAG, "socket 创建失败 errno=%d", errno);
        goto out;
    }
    if (bind(sock, (struct sockaddr *)&dest, sizeof(dest)) < 0) {
        ESP_LOGE(TAG, "bind 53 端口失败 errno=%d", errno);
        close(sock);
        goto out;
    }
    /* 收包超时 1 秒：这样 dns_redirect_stop() 不用强行删任务，
     * 任务自己醒来发现 s_running=false 就能把 socket 关干净。
     * 强行 vTaskDelete 会漏掉 close，配网多进几次就把 LWIP 的 8 个
     * socket 耗光了（实测后面连 HTTP 都开不出来）。 */
    struct timeval tv = {.tv_sec = 1, .tv_usec = 0};
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    s_sock = sock;

    ESP_LOGI(TAG, "DNS 劫持已启动，所有域名 → 热点 IP");

    while (s_running) {
        struct sockaddr_storage src;
        socklen_t slen = sizeof(src);
        int len = recvfrom(sock, rx, sizeof(rx), 0, (struct sockaddr *)&src, &slen);
        if (len < 0) {
            continue; /* 超时或出错，回上面再看一眼 s_running */
        }
        int rl = parse_dns_request(rx, len, reply, sizeof(reply));
        if (rl > 0) {
            sendto(sock, reply, rl, 0, (struct sockaddr *)&src, slen);
        }
    }

    s_sock = -1;
    close(sock);

out:
    s_running = false;
    s_task = NULL;
    vTaskDelete(NULL);
}

bool dns_redirect_start(const char *if_key)
{
    if (s_running) {
        return true;
    }
    if (s_ap_ip == 0) {
        ESP_LOGE(TAG, "热点 IP 未知，先启动 AP 再开 DNS 劫持");
        return false;
    }
    s_running = true;
    BaseType_t r = xTaskCreate(dns_task, "dns_redirect", 4096, NULL, 5, &s_task);
    if (r != pdPASS) {
        s_running = false;
        s_task = NULL;
        ESP_LOGE(TAG, "DNS 任务创建失败");
        return false;
    }
    (void)if_key;
    return true;
}

void dns_redirect_stop(void)
{
    if (!s_running) {
        return;
    }
    s_running = false;
    /* 任务最多 1 秒就自己醒来关 socket，等它一下 */
    for (int i = 0; i < 20 && s_task; i++) {
        vTaskDelay(pdMS_TO_TICKS(100));
    }
    if (s_task) {
        ESP_LOGW(TAG, "DNS 任务没自己退出，兜底删除");
        vTaskDelete(s_task);
        s_task = NULL;
        if (s_sock >= 0) {
            close(s_sock);
            s_sock = -1;
        }
    }
    ESP_LOGI(TAG, "DNS 劫持已停止");
}

bool dns_redirect_running(void)
{
    return s_running;
}

/* 供 netcfg 在 AP 起来后调用：把热点 IP 记下来 */
void dns_redirect_set_ip(uint32_t ip_network_order)
{
    s_ap_ip = ip_network_order;
}

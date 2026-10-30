#include "ogg_mux.h"

#include <string.h>

static uint32_t s_crc_table[256];
static bool s_crc_ready;

static void crc_init(void)
{
    for (uint32_t i = 0; i < 256; i++) {
        uint32_t r = i << 24;
        for (int k = 0; k < 8; k++) {
            r = (r & 0x80000000u) ? ((r << 1) ^ 0x04C11DB7u) : (r << 1);
        }
        s_crc_table[i] = r;
    }
    s_crc_ready = true;
}

/* Ogg 用的 CRC32：poly 0x04C11DB7，无反射，初值 0，无终值异或 */
static uint32_t crc_update(uint32_t crc, const uint8_t *data, size_t n)
{
    for (size_t i = 0; i < n; i++) {
        crc = (crc << 8) ^ s_crc_table[((crc >> 24) & 0xFF) ^ data[i]];
    }
    return crc;
}

static void put_le16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v & 0xFF);
    p[1] = (uint8_t)(v >> 8);
}

static void put_le32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v & 0xFF);
    p[1] = (uint8_t)((v >> 8) & 0xFF);
    p[2] = (uint8_t)((v >> 16) & 0xFF);
    p[3] = (uint8_t)((v >> 24) & 0xFF);
}

static void put_le64(uint8_t *p, uint64_t v)
{
    for (int i = 0; i < 8; i++) {
        p[i] = (uint8_t)((v >> (8 * i)) & 0xFF);
    }
}

static bool page_flush(ogg_mux_t *m, uint8_t header_type, uint64_t granule)
{
    if (!m->fp) {
        return false;
    }
    uint8_t hdr[27 + OGG_MAX_SEGS];
    memcpy(hdr, "OggS", 4);
    hdr[4] = 0; /* version */
    hdr[5] = header_type;
    put_le64(hdr + 6, granule);
    put_le32(hdr + 14, m->serial);
    put_le32(hdr + 18, m->page_seq++);
    put_le32(hdr + 22, 0); /* CRC 先占位 */
    hdr[26] = (uint8_t)m->nseg;
    memcpy(hdr + 27, m->segs, (size_t)m->nseg);

    size_t hdr_len = 27 + (size_t)m->nseg;
    uint32_t crc = 0;
    crc = crc_update(crc, hdr, hdr_len);
    if (m->page_len) {
        crc = crc_update(crc, m->page, m->page_len);
    }
    put_le32(hdr + 22, crc);

    bool ok = fwrite(hdr, 1, hdr_len, m->fp) == hdr_len;
    if (ok && m->page_len) {
        ok = fwrite(m->page, 1, m->page_len, m->fp) == m->page_len;
    }
    m->total_bytes += (uint32_t)hdr_len + m->page_len;
    m->page_len = 0;
    m->nseg = 0;
    return ok;
}

void ogg_mux_init(ogg_mux_t *m, uint16_t channels, uint32_t input_rate, uint16_t pre_skip,
                  uint32_t samples_per_pkt)
{
    memset(m, 0, sizeof(*m));
    m->channels = channels;
    m->input_rate = input_rate;
    m->pre_skip = pre_skip;
    m->samples_per_pkt = samples_per_pkt;
    if (!s_crc_ready) {
        crc_init();
    }
}

bool ogg_mux_begin(ogg_mux_t *m, FILE *fp, uint32_t serial)
{
    if (!fp) {
        return false;
    }
    m->fp = fp;
    m->serial = serial;

    /* ---- OpusHead（19 字节，必须独占 BOS 页） ---- */
    uint8_t head[19];
    memcpy(head, "OpusHead", 8);
    head[8] = 1;                    /* version */
    head[9] = (uint8_t)m->channels;
    put_le16(head + 10, m->pre_skip);
    put_le32(head + 12, m->input_rate);
    put_le16(head + 16, 0);         /* output gain */
    head[18] = 0;                   /* mapping family 0 = 单/双声道 */

    m->nseg = 0;
    m->segs[m->nseg++] = sizeof(head);
    memcpy(m->page, head, sizeof(head));
    m->page_len = sizeof(head);
    if (!page_flush(m, 0x02 /* BOS */, 0)) {
        return false;
    }

    /* ---- OpusTags ---- */
    static const char vendor[] = "recall-mic";
    uint8_t tags[8 + 4 + sizeof(vendor) - 1 + 4];
    size_t n = 0;
    memcpy(tags + n, "OpusTags", 8);
    n += 8;
    put_le32(tags + n, (uint32_t)(sizeof(vendor) - 1));
    n += 4;
    memcpy(tags + n, vendor, sizeof(vendor) - 1);
    n += sizeof(vendor) - 1;
    put_le32(tags + n, 0); /* 0 条 comment */
    n += 4;

    m->nseg = 0;
    m->segs[m->nseg++] = (uint8_t)n;
    memcpy(m->page, tags, n);
    m->page_len = (uint32_t)n;
    if (!page_flush(m, 0x00, 0)) {
        return false;
    }

    m->started = true;
    return true;
}

bool ogg_mux_add(ogg_mux_t *m, const uint8_t *pkt, uint32_t len)
{
    if (!m->started || m->finished || !pkt || len == 0) {
        return false;
    }
    /* 单包不跨页：20ms 的 Opus 包最多几百字节，永远不会超 */
    if (len > OGG_MAX_PAYLOAD) {
        return false;
    }
    if (m->nseg + (int)(len / 255) + 1 > OGG_MAX_SEGS ||
        m->page_len + len > OGG_MAX_PAYLOAD) {
        if (!page_flush(m, 0x00, m->pre_skip + m->granule)) {
            return false;
        }
    }
    if (m->nseg >= OGG_PACKETS_PER_PAGE) {
        if (!page_flush(m, 0x00, m->pre_skip + m->granule)) {
            return false;
        }
    }

    /* lacing：每 255 字节一个 255，余数单独一个值；整 255 倍数时补 0 */
    uint32_t left = len;
    while (left >= 255) {
        m->segs[m->nseg++] = 255;
        left -= 255;
    }
    m->segs[m->nseg++] = (uint8_t)left;

    memcpy(m->page + m->page_len, pkt, len);
    m->page_len += len;
    m->granule += m->samples_per_pkt;
    return true;
}

bool ogg_mux_end(ogg_mux_t *m)
{
    if (!m->started || m->finished) {
        return false;
    }
    bool ok = true;
    if (m->nseg > 0 || m->page_len > 0) {
        ok = page_flush(m, 0x04 /* EOS */, m->pre_skip + m->granule);
    } else {
        ok = page_flush(m, 0x04, m->pre_skip + m->granule);
    }
    fflush(m->fp);
    m->finished = true;
    return ok;
}

#include "ogg_opus.h"

#include <string.h>

#include "esp_audio_enc.h"
#include "esp_log.h"
#include "esp_opus_enc.h"
#include "esp_random.h"

static const char *TAG = "opus";

static void *s_enc;
static int s_in_size;  /* 一帧 PCM 字节数 */
static int s_out_size; /* 一帧压缩后推荐缓冲 */
static uint8_t *s_out_buf;

esp_err_t ogg_opus_init(void)
{
    if (s_enc) {
        return ESP_OK;
    }

    esp_opus_enc_config_t cfg = ESP_OPUS_ENC_CONFIG_DEFAULT();
    cfg.sample_rate = APP_SAMPLE_RATE;
    cfg.channel = 1;
    cfg.bits_per_sample = 16;
    cfg.bitrate = CONFIG_RECALL_OPUS_BITRATE;
    cfg.frame_duration = ESP_OPUS_ENC_FRAME_DURATION_20_MS;
    cfg.application_mode = ESP_OPUS_ENC_APPLICATION_VOIP;
    cfg.complexity = CONFIG_RECALL_OPUS_COMPLEXITY;
    cfg.enable_fec = false;
    cfg.enable_dtx = false; /* 静音由 VAD 丢掉，不依赖 DTX */
    cfg.enable_vbr = true;

    esp_audio_err_t ret = esp_opus_enc_open(&cfg, sizeof(cfg), &s_enc);
    if (ret != ESP_AUDIO_ERR_OK) {
        ESP_LOGE(TAG, "opus encoder open failed: %d", ret);
        s_enc = NULL;
        return ESP_FAIL;
    }
    ret = esp_opus_enc_get_frame_size(s_enc, &s_in_size, &s_out_size);
    if (ret != ESP_AUDIO_ERR_OK) {
        ESP_LOGE(TAG, "opus get_frame_size failed: %d", ret);
        return ESP_FAIL;
    }
    s_out_buf = malloc(s_out_size);
    if (!s_out_buf) {
        ESP_LOGE(TAG, "no mem for opus out buffer (%d)", s_out_size);
        return ESP_ERR_NO_MEM;
    }
    ESP_LOGI(TAG, "opus ready: %dHz mono %dbps c=%d, in %d / out %d bytes per frame",
             APP_SAMPLE_RATE, CONFIG_RECALL_OPUS_BITRATE, CONFIG_RECALL_OPUS_COMPLEXITY, s_in_size,
             s_out_size);
    return ESP_OK;
}

void ogg_opus_deinit(void)
{
    if (s_enc) {
        esp_opus_enc_close(s_enc);
        s_enc = NULL;
    }
    if (s_out_buf) {
        free(s_out_buf);
        s_out_buf = NULL;
    }
}

esp_err_t ogg_opus_begin(ogg_opus_rec_t *r, FILE *fp)
{
    if (!s_enc || !fp) {
        return ESP_ERR_INVALID_STATE;
    }
    memset(r, 0, sizeof(*r));
    ogg_mux_init(&r->mux, 1, APP_SAMPLE_RATE, OGG_PRE_SKIP, OGG_SAMPLES_PER_PKT);
    if (!ogg_mux_begin(&r->mux, fp, esp_random())) {
        return ESP_FAIL;
    }
    r->active = true;
    return ESP_OK;
}

static void encode_one(ogg_opus_rec_t *r)
{
    esp_audio_enc_in_frame_t in = {
        .buffer = (uint8_t *)r->pcm,
        .len = (uint32_t)s_in_size,
    };
    esp_audio_enc_out_frame_t out = {
        .buffer = s_out_buf,
        .len = (uint32_t)s_out_size,
    };
    esp_audio_err_t ret = esp_opus_enc_process(s_enc, &in, &out);
    if (ret != ESP_AUDIO_ERR_OK) {
        ESP_LOGW(TAG, "opus encode failed: %d", ret);
        return;
    }
    if (out.encoded_bytes) {
        ogg_mux_add(&r->mux, s_out_buf, out.encoded_bytes);
    }
}

void ogg_opus_write(ogg_opus_rec_t *r, const int16_t *pcm, int n)
{
    if (!r->active || n <= 0) {
        return;
    }
    int i = 0;
    while (i < n) {
        int free_slot = APP_FRAME_SAMPLES - r->pcm_fill;
        int take = (n - i) < free_slot ? (n - i) : free_slot;
        memcpy(r->pcm + r->pcm_fill, pcm + i, (size_t)take * sizeof(int16_t));
        r->pcm_fill += take;
        i += take;
        if (r->pcm_fill == APP_FRAME_SAMPLES) {
            encode_one(r);
            r->pcm_fill = 0;
        }
    }
}

uint32_t ogg_opus_end(ogg_opus_rec_t *r)
{
    if (!r->active) {
        return 0;
    }
    if (r->pcm_fill > 0) {
        memset(r->pcm + r->pcm_fill, 0, (size_t)(APP_FRAME_SAMPLES - r->pcm_fill) * sizeof(int16_t));
        r->pcm_fill = APP_FRAME_SAMPLES;
        encode_one(r);
        r->pcm_fill = 0;
    }
    ogg_mux_end(&r->mux);
    fflush(r->mux.fp);
    r->active = false;
    return r->mux.total_bytes;
}

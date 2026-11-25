#include "vad.h"

#include <math.h>
#include <string.h>

#include "app_config.h"
#include "esp_log.h"
#include "sr_vad.h"

static const char *TAG = "vad";

void mvad_init(vad_t *v, uint32_t frame_ms)
{
    memset(v, 0, sizeof(*v));
    v->frame_ms = frame_ms ? frame_ms : APP_FRAME_MS;
    /* 模式见 app_config.h 的 VAD_SR_MODE：数值越大越不肯认人声。
     * 当前用 1（Aggressive）。说话开头、正常音量老是不触发就降到 0；
     * 安静环境下反而误触发多，再往 2 调。 */
    v->sr = sr_vad_create(VAD_SR_MODE);
    if (!v->sr) {
        ESP_LOGE(TAG, "ESP-SR VAD 创建失败，人声检测不可用");
    }
    v->noise_rms = 60.0f; /* 初始噪声底，几帧内就会跟上真实环境 */
}

void mvad_reset_segment(vad_t *v)
{
    /* 注意：noise_rms 故意不重置——噪声底要跨片段/跨会话延续，
     * 每段重来会让开头几帧的闸门失真 */
    v->voiced_ms = 0;
    v->half_ms = 0;
    v->total_ms = 0;
    v->peak_rms = 0;
    v->speech = false;
    v->speech_run = 0;
    v->silence_run = 0;
}

int mvad_level(const vad_t *v)
{
    /* 用 dB 映射到 0~100，安静时也有细微变化 */
    float db = 20.0f * log10f((v->last_rms > 1.0f ? v->last_rms : 1.0f) / 60.0f);
    float pct = db * (100.0f / 40.0f); /* 40dB 跨度满格 */
    if (pct < 0) {
        pct = 0;
    }
    if (pct > 100) {
        pct = 100;
    }
    return (int)pct;
}

bool mvad_process(vad_t *v, const int16_t *pcm, int n)
{
    if (n <= 0) {
        return v->speech;
    }

    /* RMS 只用于界面上的电平条，判定交给 ESP-SR */
    int64_t acc = 0;
    for (int i = 0; i < n; i++) {
        int32_t s = pcm[i];
        acc += (int64_t)s * s;
    }
    float rms = sqrtf((float)acc / (float)n);
    v->last_rms = rms;
    if (rms > v->peak_rms) {
        v->peak_rms = rms;
    }

    /* 注意：sr_vad_run 会按 16kHz/20ms 重新切帧，所以 n 必须正好是 320 */
    bool raw = sr_vad_run((sr_vad_t)v->sr, (int16_t *)pcm);

    /* 自适应噪声底：更安静时快速降（0.8/0.2），VAD 判静音时极慢上浮
     * （适应环境变吵），VAD 说是人声的帧不动，免得把说话算进噪声底 */
    if (v->noise_rms <= 1.0f) {
        v->noise_rms = rms;
    } else if (rms < v->noise_rms) {
        v->noise_rms = v->noise_rms * 0.8f + rms * 0.2f;
    } else if (!raw) {
        v->noise_rms = v->noise_rms * 0.995f + rms * 0.005f;
    }
    float gate = VAD_RMS_ABS_MIN;
    float adapt = v->noise_rms * VAD_NOISE_FACTOR + VAD_NOISE_MARGIN;
    if (adapt > gate) {
        gate = adapt;
    }
    v->last_gate = gate;
    /* 双条件：WebRTC 说像人声 + 能量确实高于底噪。只看 VAD 会被底噪骗 */
    bool above = raw && (rms >= gate);
    v->last_above = above;
    v->total_ms += v->frame_ms;

    if (above) {
        v->speech_run++;
        v->silence_run = 0;
        /* 连续够久才起判：单帧抖动不算（也顺便挡掉敲击这种极短瞬态） */
        if (!v->speech && v->speech_run >= VAD_TRIGGER_FRAMES) {
            v->speech = true;
        }
    } else {
        v->speech_run = 0;
        v->silence_run++;
    }

    if (v->speech) {
        if (above) {
            v->voiced_ms += v->frame_ms;
        } else {
            v->half_ms += v->frame_ms;
            /* 拖尾：连续静音够久才真正结束，避免把尾音切掉 */
            if ((uint32_t)v->silence_run * v->frame_ms >= VAD_HANGOVER_MS) {
                v->speech = false;
                v->silence_run = 0;
            }
        }
    }

    return v->speech;
}

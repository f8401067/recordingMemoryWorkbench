#include "vad.h"

#include <math.h>
#include <string.h>

#include "app_config.h"

void vad_init(vad_t *v, uint32_t frame_ms)
{
    memset(v, 0, sizeof(*v));
    v->frame_ms = frame_ms ? frame_ms : APP_FRAME_MS;
    /* 起始噪声底取一个保守值，前 2 秒会很快跟上真实环境 */
    v->noise_floor = 120.0f;
}

void vad_reset_segment(vad_t *v)
{
    v->voiced_ms = 0;
    v->half_ms = 0;
    v->total_ms = 0;
    v->peak_rms = 0;
    v->speech = false;
    v->speech_run = 0;
    v->silence_run = 0;
}

int vad_level(const vad_t *v)
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

bool vad_process(vad_t *v, const int16_t *pcm, int n)
{
    if (n <= 0) {
        return v->speech;
    }

    int64_t acc = 0;
    int zc = 0;
    int16_t prev = pcm[0];
    for (int i = 0; i < n; i++) {
        int32_t s = pcm[i];
        acc += (int64_t)s * s;
        if ((s >= 0) != (prev >= 0)) {
            zc++;
        }
        prev = (int16_t)s;
    }

    float rms = sqrtf((float)acc / (float)n);
    float zcr = (float)zc / (float)n;
    v->last_rms = rms;
    v->last_zcr = zcr;

    float dyn = v->noise_floor * VAD_TRIGGER_RATIO;
    if (dyn < VAD_MIN_RMS) {
        dyn = VAD_MIN_RMS;
    }

    /* 过零率太高的多半是摩擦声/键盘声，不当人声，但可以用来更新噪声底 */
    bool above = (rms > dyn) && (zcr < VAD_MAX_ZCR);

    /* 噪声底只在"非语音"时跟随，避免人声把门限越抬越高 */
    if (!v->speech && !above) {
        v->noise_floor = (1.0f - VAD_NOISE_ALPHA) * v->noise_floor + VAD_NOISE_ALPHA * rms;
        if (v->noise_floor < 20.0f) {
            v->noise_floor = 20.0f;
        }
        if (v->noise_floor > 4000.0f) {
            v->noise_floor = 4000.0f;
        }
    }

    v->total_ms += v->frame_ms;

    if (above) {
        v->speech_run++;
        v->silence_run = 0;
        if (rms > v->peak_rms) {
            v->peak_rms = rms;
        }
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
            if ((uint32_t)v->silence_run * v->frame_ms >= VAD_HANGOVER_MS) {
                v->speech = false;
                v->silence_run = 0;
            }
        }
    }

    return v->speech;
}

#include "sr_vad.h"

#include <stdlib.h>

#include "esp_vad.h"

struct sr_vad_s {
    vad_handle_t inst;
};

sr_vad_t sr_vad_create(int mode)
{
    struct sr_vad_s *w = calloc(1, sizeof(*w));
    if (!w) {
        return NULL;
    }
    w->inst = vad_create((vad_mode_t)mode);
    if (!w->inst) {
        free(w);
        return NULL;
    }
    return w;
}

bool sr_vad_run(sr_vad_t h, int16_t *pcm)
{
    if (!h || !h->inst || !pcm) {
        return false;
    }
    return vad_process(h->inst, pcm, 16000, 20) == VAD_SPEECH;
}

void sr_vad_destroy(sr_vad_t h)
{
    if (!h) {
        return;
    }
    if (h->inst) {
        vad_destroy(h->inst);
    }
    free(h);
}

#include "chime.h"
#include <string.h>
#include <Arduino.h>
#include <math.h>
#include "ESP_I2S.h"
#include "es8311.h"
#include "bell_pcm.h"   // const uint8_t bell_pcm[] / bell_pcm_len — 44.1 kHz 16-bit stereo

// Shared ES8311 chime engine. See chime.h. Adapted from the original 2.16
// sound.cpp so the 2.16, 1.8 (and any future ES8311 board) share one copy of
// the codec setup, the embedded PCM, and the non-blocking playback task.

static I2SClass      i2s;
static ChimeConfig   cfg;
static bool          ready   = false;
static volatile bool playing = false;
static volatile uint16_t gain_q8 = 256;   // sample gain, 256 = 100 % (chime_set_gain)

void chime_set_gain(uint8_t pct) {
    if (pct > 100) pct = 100;
    gain_q8 = (uint16_t)(pct * 256 / 100);
}

static bool es8311_setup(void) {
    es8311_handle_t es = es8311_create(0, cfg.es8311_addr);   // I2C port 0 (shared Wire bus)
    if (!es) return false;
    // mclk_inverted, sclk_inverted, mclk_from_mclk_pin, mclk_frequency, sample_frequency
    const es8311_clock_config_t clk = {
        false, false, true, cfg.sample_rate * 256, cfg.sample_rate
    };
    if (es8311_init(es, &clk, ES8311_RESOLUTION_16, ES8311_RESOLUTION_16) != ESP_OK) return false;
    es8311_sample_frequency_config(es, clk.mclk_frequency, clk.sample_frequency);
    es8311_microphone_config(es, false);
    es8311_voice_volume_set(es, cfg.volume, NULL);
    return true;
}

#define CUE_AMP_SETTLE_MS 150  // power-amp start-up before the first note

static void chime_task(void* arg) {
    if (cfg.amp_enable) cfg.amp_enable(true);
    delay(CUE_AMP_SETTLE_MS);                  // amp start-up, else the strike is lost
    const uint16_t g = gain_q8;
    if (g >= 256) {
        i2s.write((uint8_t*)bell_pcm, bell_pcm_len);
    } else {                                   // scale through a small buffer
        static int16_t buf[512];
        const size_t   n = bell_pcm_len / 2;   // samples; bytes may be unaligned
        for (size_t done = 0; done < n; ) {
            size_t chunk = n - done;
            if (chunk > 512) chunk = 512;
            for (size_t i = 0; i < chunk; i++) {
                const uint8_t* b = &bell_pcm[(done + i) * 2];
                const int16_t  v = (int16_t)(b[0] | (b[1] << 8));   // little-endian
                buf[i] = (int16_t)(((int32_t)v * g) >> 8);
            }
            i2s.write((uint8_t*)buf, chunk * 2);
            done += chunk;
        }
    }
    delay(20);
    if (cfg.amp_enable) cfg.amp_enable(false);
    playing = false;
    vTaskDelete(nullptr);
}

bool chime_init(const ChimeConfig& c) {
    cfg = c;
    if (cfg.amp_enable) cfg.amp_enable(false);   // amp off until we play

    i2s.setPins(cfg.bclk, cfg.ws, cfg.dout, cfg.din, cfg.mclk);
    if (!i2s.begin(I2S_MODE_STD, cfg.sample_rate, I2S_DATA_BIT_WIDTH_16BIT,
                   I2S_SLOT_MODE_STEREO, I2S_STD_SLOT_BOTH)) {
        Serial.println("chime: I2S init failed");
        return false;
    }
    if (!es8311_setup()) {
        Serial.println("chime: ES8311 init failed");
        return false;
    }
    ready = true;
    Serial.println("chime: ES8311 ready");
    return true;
}

void chime_play(void) {
    if (!ready || playing) return;
    playing = true;
    if (xTaskCreatePinnedToCore(chime_task, "chime", 4096, nullptr, 1, nullptr, 0) != pdPASS)
        playing = false;   // couldn't spawn — stay silent rather than wedge the flag
}

// ---- Synthesized UI cues (see chime.h) ----

struct cue_note { uint16_t freq_hz; uint16_t ms; };

// freq_hz 0 = a rest (silence) between notes.
static const cue_note cue_armed[]     = { {880, 90} };
static const cue_note cue_paired[]    = { {660, 70}, {988, 130} };
static const cue_note cue_needs_you[] = { {880, 80}, {0, 70}, {880, 80} };
static const cue_note cue_your_turn[] = { {659, 90}, {831, 90}, {988, 180} };   // E major, rising
static const cue_note cue_limit[]     = { {523, 140}, {392, 140}, {262, 240} };
#define CUE_SET(a) do { seq = a; len = (uint8_t)(sizeof(a) / sizeof(a[0])); } while (0)

static const cue_note* cue_seq = nullptr;
static uint8_t         cue_len = 0;

#define CUE_AMPLITUDE 9000    // ~28% of full scale — audible, no amp strain
#define CUE_EDGE_MS   4       // attack/release ramp; without it the amp clicks
#define CUE_TAIL_MS   120     // silence pushed behind a cue to flush the DMA ring

static void cue_task(void* arg) {
    if (cfg.amp_enable) cfg.amp_enable(true);
    // The power amp takes far longer than the bell's 8 ms to come up: on the
    // LCD-1.54 the 90 ms "armed" blip vanished entirely and of the two-tone
    // "paired" cue only the second note was heard. The bell is long enough to
    // hide a lost attack; a cue is not, so wait until the amp is surely live.
    delay(CUE_AMP_SETTLE_MS);

    static int16_t frames[256 * 2];            // stereo scratch, one cue at a time
    const uint32_t edge = (uint32_t)cfg.sample_rate * CUE_EDGE_MS / 1000;
    const uint32_t t0   = millis();
    uint32_t       cue_ms = 0;

    for (uint8_t n = 0; n < cue_len; n++) {
        const uint32_t total = (uint32_t)cfg.sample_rate * cue_seq[n].ms / 1000;
        const float    step  = 2.0f * (float)M_PI * cue_seq[n].freq_hz / cfg.sample_rate;
        const float    amp   = cue_seq[n].freq_hz                         // 0 Hz = rest
                               ? CUE_AMPLITUDE * gain_q8 / 256.0f : 0.0f;
        float          phase = 0.0f;
        cue_ms += cue_seq[n].ms;
        for (uint32_t done = 0; done < total; ) {
            uint32_t chunk = total - done;
            if (chunk > 256) chunk = 256;
            for (uint32_t i = 0; i < chunk; i++) {
                const uint32_t pos = done + i;
                float env = 1.0f;
                if (pos < edge)                env = (float)pos / (float)edge;
                else if (total - pos < edge)   env = (float)(total - pos) / (float)edge;
                const int16_t s = (int16_t)(sinf(phase) * amp * env);
                phase += step;
                frames[i * 2]     = s;
                frames[i * 2 + 1] = s;
            }
            i2s.write((uint8_t*)frames, chunk * 4);   // 2 ch x 16 bit
            done += chunk;
        }
    }

    // i2s.write() returns once the samples are in the DMA buffers, not once
    // they have played — and a whole 90 ms blip fits in them. Switching the
    // amp off 20 ms later cut the short cues to nothing (only the 200 ms
    // two-tone was half audible). So push silence behind the tone to flush it
    // through the DMA ring, and keep the amp on until the tone's own duration
    // has passed on the wall clock.
    memset(frames, 0, sizeof(frames));
    for (uint32_t done = 0, pad = (uint32_t)cfg.sample_rate * CUE_TAIL_MS / 1000; done < pad; ) {
        uint32_t chunk = pad - done;
        if (chunk > 256) chunk = 256;
        i2s.write((uint8_t*)frames, chunk * 4);
        done += chunk;
    }
    while (millis() - t0 < cue_ms + CUE_TAIL_MS) delay(5);
    if (cfg.amp_enable) cfg.amp_enable(false);
    playing = false;
    vTaskDelete(nullptr);
}

void chime_play_cue(chime_cue_t cue) {
    if (!ready || playing) return;
    const cue_note* seq; uint8_t len;
    switch (cue) {
        case CHIME_CUE_PAIRED:    CUE_SET(cue_paired);    break;
        case CHIME_CUE_NEEDS_YOU: CUE_SET(cue_needs_you); break;
        case CHIME_CUE_YOUR_TURN: CUE_SET(cue_your_turn); break;
        case CHIME_CUE_LIMIT:     CUE_SET(cue_limit);     break;
        default:                  CUE_SET(cue_armed);     break;
    }
    cue_seq = seq; cue_len = len;
    playing = true;
    if (xTaskCreatePinnedToCore(cue_task, "chime_cue", 4096, nullptr, 1, nullptr, 0) != pdPASS)
        playing = false;   // couldn't spawn — stay silent rather than wedge the flag
}

void chime_tick(void) {}   // playback runs in chime_task; nothing to poll

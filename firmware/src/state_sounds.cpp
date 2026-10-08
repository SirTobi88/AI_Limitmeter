#include "state_sounds.h"
#include "hal/sound_hal.h"
#include <Arduino.h>
#include <string.h>

#define NEEDS_YOU_REMIND_MS 120000UL   // one reminder if you haven't reacted

static char     prev[24]      = "";
static bool     have_prev     = false;   // false until the first state arrives
static bool     enabled_now   = false;
static bool     remind        = false;   // a NEEDS_YOU reminder is pending
static uint32_t remind_at     = 0;

static bool is(const char* s, const char* name) { return strcmp(s, name) == 0; }

static bool was_busy(const char* s) {
    return is(s, "work coding") || is(s, "write") || is(s, "work think") ||
           is(s, "think") || is(s, "allow");
}

bool state_sounds_on_state(const char* host_state, bool enabled) {
    const char* s = host_state ? host_state : "";
    enabled_now = enabled;
    if (have_prev && is(s, prev)) return false;      // no change

    const bool needs_you = have_prev && is(s, "allow");
    const bool your_turn = have_prev && is(s, "done") && was_busy(prev);

    if (have_prev && enabled) {
        if (needs_you) {
            sound_hal_play_state(SOUND_STATE_NEEDS_YOU);
        } else if (your_turn) {
            sound_hal_play_state(SOUND_STATE_YOUR_TURN);
        } else if (is(s, "limit")) {
            sound_hal_play_state(SOUND_STATE_LIMIT);
        }
    }
    remind    = is(s, "allow");
    remind_at = millis() + NEEDS_YOU_REMIND_MS;

    strlcpy(prev, s, sizeof(prev));
    have_prev = true;
    return needs_you || your_turn;
}

void state_sounds_tick(void) {
    if (!remind || !enabled_now) return;
    if ((int32_t)(millis() - remind_at) < 0) return;
    remind = false;                                  // once only
    sound_hal_play_state(SOUND_STATE_NEEDS_YOU);
}

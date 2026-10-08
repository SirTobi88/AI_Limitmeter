#pragma once

// Cues for Claude Code state changes, driven by the host's state name (BLE
// payload field "a"). The daemon sets "ss" unless its config says
// `state_sounds = off` (and only while it sends Claude Code's state at all).
//
//   allow  -> NEEDS_YOU, and once more after 2 min if still waiting
//   done   -> YOUR_TURN, only when Claude was working (or asking) before —
//             not on boot, reconnect or a stale "done"
//   limit  -> LIMIT, once on reaching it
//
// The first state after boot is only recorded, never sounded.
//
// Returns true when this change wants the user — "allow" (Needs you) or a
// "done" that ends a turn (Your turn), by the same rules as the sounds but
// whether or not they are enabled. main.cpp wakes the screen on it.
bool state_sounds_on_state(const char* host_state, bool enabled);
void state_sounds_tick(void);

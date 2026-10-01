#pragma once
#include <stdint.h>
#include <lvgl.h>

// Initialize splash module. Creates the canvas widget inside `parent` and
// allocates the 480x480 pixel buffer (PSRAM).
void splash_init(lv_obj_t *parent);

// Advance animation frame if hold time elapsed. Call from main loop.
void splash_tick(void);

// Cycle to the next / previous animation in the catalog.
void splash_next(void);
void splash_prev(void);

// Host-driven animation (BLE payload field "a"). `name` is either an official
// animation name ("laptop") or one of the host state names the daemon and the
// Session Browser send ("work coding", "allow", "done", ...), which map onto
// the official set (see HOST_ALIASES in splash.cpp). "" or NULL — or an idle
// state name — hands control back to splash_pick_for_current_rate(). Only acts
// when the requested name changes; the switch waits for the running
// animation's outro, so it always passes through the shared idle pose.
void splash_set_anim(const char *name);

// Official name of the animation currently on the splash.
const char* splash_current_anim_name(void);

// Ask for a complete repaint of the splash. On the direct-draw boards (no
// PSRAM) the repaint waits for LVGL to finish a full pass first, so the black
// container fill can't wipe half the creature; a no-op on the canvas path.
void splash_request_full_redraw(void);

// Called from the display flush callback when LVGL has flushed the last strip
// of a refresh pass (see splash_request_full_redraw). Exists on every board.
void splash_note_refresh_done(void);

// Show/hide the splash container.
void splash_show(void);
void splash_hide(void);

// Pick the next animation matching the current usage-rate group.
// Called automatically by splash_show(); also exposed so other modules can
// trigger a re-pick when the rate group changes mid-display.
void splash_pick_for_current_rate(void);

// True when splash is currently rendering (used to gate re-picks).
bool splash_is_active(void);

// Root container (so ui.cpp can attach a click event).
lv_obj_t* splash_get_root(void);

// Mini animated creature for embedding elsewhere (e.g. the idle screen).
// Renders the named official animation (e.g. "cloud") at ~px×px
// inside `parent`; returns the canvas object (position it with lv_obj_align) or
// NULL if the animation isn't found / allocation fails. Drive it with
// splash_mini_tick(). One mini creature at a time.
lv_obj_t* splash_mini_create(lv_obj_t *parent, const char *anim_name, int px);
void splash_mini_tick(void);

// Corner mascot (usage screen, PSRAM boards): the still Clawd idles in the
// logo slot, does occasional acts, and takes walk-off/lurk/walk-back trips.
// feet_y = px of the art's ground line; cell = px per art cell in the corner;
// max_w = px a host state may take to the right of slot_x before it would
// run into the title (0 = no limit) — wider states drop to a smaller cell.
lv_obj_t* splash_mascot_create(lv_obj_t *parent, int slot_x, int feet_y, int cell, int max_w);
void splash_mascot_tick(void);
void splash_mascot_set_visible(bool v);

// Host-driven corner (BLE payload field "ua"): play this animation in place
// in the corner instead of the usage-rate routine. Same names and aliases as
// splash_set_anim(); "" or NULL returns the mascot to its own routine.
void splash_mascot_set_host_anim(const char *name);

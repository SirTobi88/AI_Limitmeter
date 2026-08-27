#pragma once
#include <stdint.h>
#include <lvgl.h>

// Initialize splash module. Creates the canvas widget inside `parent` and
// allocates the 480x480 pixel buffer (PSRAM).
void splash_init(lv_obj_t *parent);

// Advance animation frame if hold time elapsed. Call from main loop.
void splash_tick(void);

// Cycle to the next animation in the catalog.
void splash_next(void);

// Show/hide the splash container.
void splash_show(void);
void splash_hide(void);

// Repaint every cell on the next tick instead of only the ones that changed.
// Needed whenever something else has drawn over the splash — a screen switch,
// or an overlay that has just gone away. Without it the covered part stays
// black until the animation happens to change those cells, which on a mostly
// still frame can be never.
void splash_request_full_redraw(void);

// Pick the next animation matching the current usage-rate group.
// Called automatically by splash_show(); also exposed so other modules can
// trigger a re-pick when the rate group changes mid-display.
void splash_pick_for_current_rate(void);

// Let the host drive the animation instead of the usage-rate heuristic.
// `name` is a splash_anims[] name (e.g. "work coding"); "" or NULL hands
// control back to splash_pick_for_current_rate(). Only acts when the requested
// name actually changes, so a host repeating the same name every poll doesn't
// fight the PWR button.
void splash_set_anim(const char *name);

// Wie die Animation heisst, die das Geraet gerade selbst gewaehlt hat. Fuer
// alles, was sich danach richten will, ohne den Splash zu zeichnen -- etwa
// der kleine Buddy in der Ecke des Usage-Screens.
const char* splash_current_anim_name(void);

// Aus dem LVGL-Flush-Callback zu rufen, sobald der letzte Streifen eines
// Bilddurchlaufs draussen ist. Der Splash malt auf manchen Boards direkt auf
// den Panel und muss wissen, wann LVGL fertig ist - sonst uebermalt ein noch
// laufender Durchlauf das gerade Gezeichnete.
void splash_note_refresh_done(void);

// True when splash is currently rendering (used to gate re-picks).
bool splash_is_active(void);

// Root container (so ui.cpp can attach a click event).
lv_obj_t* splash_get_root(void);

// Mini animated creature for embedding elsewhere (the idle screen, the corner
// of the usage screen). Renders the named claudepix animation (e.g.
// "expression sleep") at ~px×px inside `parent`.
//
// splash_mini_create() keeps the original shape: it makes one and remembers
// it, and splash_mini_tick() advances that one. Returns the canvas object
// (position it with lv_obj_align) or NULL if the animation isn't found or the
// buffer can't be allocated.
lv_obj_t* splash_mini_create(lv_obj_t *parent, const char *anim_name, int px);
void splash_mini_tick(void);

// Several at once: splash_mini_new() hands back a handle to drive yourself.
// NULL means the same as above -- unknown animation, or no memory for the
// buffer; every call below tolerates a NULL handle, so a failed creature just
// never appears. splash_mini_set_anim() keeps the current animation running if
// the name is unknown, and is a no-op if it is already showing that one.
typedef struct splash_mini splash_mini_t;
splash_mini_t* splash_mini_new(lv_obj_t *parent, const char *anim_name, int px);
bool splash_mini_set_anim(splash_mini_t *m, const char *anim_name);
void splash_mini_tick_one(splash_mini_t *m);
lv_obj_t* splash_mini_obj(splash_mini_t *m);

#pragma once
#include "data.h"
#include "ble.h"

enum screen_t {
    SCREEN_SPLASH,
    SCREEN_USAGE,
    SCREEN_COUNT,
};

void ui_init(void);
void ui_update(const UsageData* data);
void ui_tick_anim(void);
void ui_show_screen(screen_t screen);
void ui_toggle_splash(void);

// Der kleine Buddy in der Ecke des Usage-Screens, an Stelle des Logos.
// Aus = Logo wie bisher. ui_set_corner_anim() nimmt denselben Namen, den auch
// splash_set_anim() bekommt.
void ui_set_corner_creature(bool on);
void ui_set_corner_anim(const char* name);
screen_t ui_get_current_screen(void);
void ui_update_ble_status(ble_state_t state, const char* name, const char* mac);
void ui_update_battery(int percent, bool charging);

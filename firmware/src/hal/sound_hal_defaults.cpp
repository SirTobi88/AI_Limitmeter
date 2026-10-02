#include "sound_hal.h"

// Boards without a speaker don't implement sound_hal_play_state(); this weak
// default keeps them building without touching every board folder. A board
// that defines the function overrides it at link time.
__attribute__((weak)) void sound_hal_play_state(sound_state_t state) { (void)state; }
__attribute__((weak)) void sound_hal_set_volume(uint8_t pct) { (void)pct; }

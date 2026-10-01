#include "../../hal/sound_hal.h"
#include <stdio.h>

void sound_hal_init(void) {}
void sound_hal_tick(void) {}
void sound_hal_play_reset(void) { printf("[sim] chime! (session reset)\n"); }
void sound_hal_play_pair_armed(void) { printf("[sim] pair armed\n"); }
void sound_hal_play_paired(void) { printf("[sim] paired\n"); }

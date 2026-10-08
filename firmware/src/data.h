#pragma once
#include <Arduino.h>

// OpenAI Codex's two rate-limit windows, read by the daemon from Codex's
// session logs ("cx" in the payload). Rides on every payload, including the
// {"ok":false} beats sent while the Claude token is dead.
struct CodexData {
    bool valid;              // false when the payload carried no "cx"
    int  session_pct;        // 5h window 0-100
    int  session_reset_mins; // minutes until it resets; -1 = unknown / already reset
    int  weekly_pct;         // 7-day window 0-100
    int  weekly_reset_mins;
    char plan[12];           // "plus", "pro", ... ("" = not sent)
};

struct UsageData {
    float session_pct;       // utilization 0-100 (5h window Pro/Max; spending % Enterprise)
    int session_reset_mins;  // minutes until reset
    float weekly_pct;        // 7-day utilization (Pro/Max only; 0 for Enterprise)
    int weekly_reset_mins;   // minutes until weekly reset (Pro/Max only)
    char status[16];         // "allowed", "limited", etc.
    bool chime;              // play the session-reset chime; false unless daemon opts in
    bool state_sounds;       // cues on Claude Code state changes ("ss")
    int  volume;             // sound level 0..100 % ("vol"); -1 = not sent
    bool enterprise;         // true = Enterprise spending-limit account
    int time_pct;            // 0-100: fraction of billing period elapsed (Enterprise)
    int period_days;         // total billing period length in days (Enterprise)
    char reset_date[12];     // formatted reset date e.g. "Jul 1" (Enterprise)
    char anim[24];           // splash animation the host wants shown ("" = host
                             // has no opinion, device picks by usage rate)
    bool corner_anim;        // show the buddy small in the usage screen's corner
                             // instead of the logo; false unless the host asks
    int  screen_mode;        // 0 = always usage, 1 = always buddy, 2 = show the
                             // buddy briefly whenever the state changes
    long clock_epoch;        // local wall-clock epoch (s) from daemon; 0 = not provided
    int  clock_fmt;          // 12 or 24 (hour format from daemon); defaults to 24
    CodexData codex;         // Codex's limits, shown beside Claude's
    bool ok;                 // data parse succeeded
    bool valid;              // false until first successful parse
};

# Project context

**AI_Limitmeter** — a fork of Clawdmeter (cloned 2026-10-08 from
`~/Documents/Clawdmeter`, history kept; public remote, not a GitHub fork
`SirTobi88/AI_Limitmeter`) that shows **OpenAI
Codex's** limits next to Claude Code's:

- Daemons: `daemon/codex_limits.py` reads the newest `rate_limits` from Codex's
  session rollouts (`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` +
  `archived_sessions/`, or `$CODEX_HOME`), from `token_count` events:
  `primary` = 5h (300 min), `secondary` = 7d (10080 min), `resets_at` epoch,
  `plan_type`. A passed `resets_at` reads as 0 % / -1. macOS + Windows daemons
  add it as `cx` = `{s, sr, w, wr, pl}` to every write, **including the
  `{"ok":false}` beats**, and re-read it every `CODEX_TICK` (10 s) between
  polls. `daemon/tests/conftest.py` stubs the reader so tests never see the
  machine's real `~/.codex`. The bash daemon sends no `cx`.
- Firmware: `UsageData.codex` (`CodexData`, data.h), parsed in `parse_json()`.
  Screens: `SCREEN_USAGE` is now the **combo** page (both providers, compact
  rows), `SCREEN_CLAUDE` / `SCREEN_CODEX` the big detail gauges. All three live
  in `usage_container` and share title, status line, pair hint and idle view;
  `update_view_state()` picks the group per page (view_state 0 pair / 1 idle /
  2 Claude / 3 Codex / 4 combo). Detail gauges come from `build_gauge_pair()`
  (bars or rings); combo panels from `build_combo_block()`, column widths
  measured from the font at runtime.
- **Swipe:** `LV_EVENT_GESTURE` left/right turns the pages (Codex skipped while
  it has no fresh data). Two LVGL 9 facts this depends on: every child has
  `GESTURE_BUBBLE` set by default, so the containers must clear it or the
  gesture sails up to the screen object; and LVGL still sends `CLICKED` /
  `SHORT_CLICKED` on release after a gesture, so `swipe_consumed` swallows
  that release (otherwise every swipe also toggled the splash).
- Testing swipes headless: the sim's `touch.cpp` reads the SDL mouse, which the
  dummy video driver doesn't have — temporarily script the finger from an env
  var (drag x 420→60 over 200 ms at y=240), then restore the file.


ESP32-S3 / ESP32-C6 firmware for a desk-side Claude Code usage monitor. Each
supported board lives in its own `firmware/src/boards/<name>/` folder and is
selected via PlatformIO's `build_src_filter`. Adding a board means dropping in
a new folder + a new `[env:...]` block — `main.cpp`, `ui.cpp`, and `splash.cpp`
never see board-specific code. See [`docs/porting/adding-a-board.md`](docs/porting/adding-a-board.md).

Seven ports today (two SoC families, five panel sizes, AMOLED + two TFTs, one round):

- `boards/waveshare_amoled_216/` — original Waveshare ESP32-S3-Touch-AMOLED-2.16 (CO5300, 480×480 square, CST9220 touch, IMU rotation). Build env: `waveshare_amoled_216`.
- `boards/waveshare_amoled_18/` — Waveshare ESP32-S3-Touch-AMOLED-1.8 (368×448 portrait, XCA9554 IO expander). Build env: `waveshare_amoled_18`. **Two panel revisions are auto-detected at boot** (`board_rev()` in `board_init.cpp`, enum in `board_rev.h`): original = SH8601 display + FT3168 touch (0x38); later = CO5300 display + CST816 touch (0x15). One binary drives both.
- `boards/waveshare_amoled_216_c6/` — Waveshare ESP32-C6-Touch-AMOLED-2.16 (SH8601, 480×480, CST9217 touch). Build env: `waveshare_amoled_216_c6`. ESP32-C6 SoC: single-core RISC-V, **no PSRAM**, BLE 5 only.
- `boards/waveshare_amoled_18_c6/` — Waveshare ESP32-C6-Touch-AMOLED-1.8 (368×448 portrait, SH8601, FT3168 touch, TCA9554 expander). Build env: `waveshare_amoled_18_c6`. Same panel as the S3 1.8 but on the C6 SoC. All subsystems (display, touch, BOOT + PWR buttons, battery, BLE) verified on hardware.
- `boards/waveshare_amoled_206/` — Waveshare ESP32-S3-Touch-AMOLED-2.06 (CO5300, 410×502 watch form factor, FT3168 touch, no IO expander, 32 MB flash, PCF85063 RTC, ES8311 codec). Build env: `waveshare_amoled_206`. Display, touch, battery, IMU init, and BLE verified on hardware; the ES8311 chime path is not wired up (`sound.cpp` no-ops).
- `boards/waveshare_lcd_154/` — Waveshare ESP32-S3-Touch-LCD-1.54 (**ST7789 TFT** over plain 4-wire SPI, 240×240, CST816 touch, no PMU, ES8311 speaker). Build env: `waveshare_lcd_154`. The only non-AMOLED port and the only one below 300 px, which is why `compute_layout()` has a "small" breakpoint.
- `boards/waveshare_knob_18/` — Waveshare ESP32-S3-Knob-Touch-LCD-1.8 (**round** 360×360 ST77916 TFT over QSPI, CST816 touch, rotary ring, DRV2605 haptics, no PMU). Build env: `waveshare_knob_18`. The only round panel (`BoardCaps.is_round` → ring-gauge layout) and the only board with a rotary ring (`has_encoder`).
- `boards/waveshare_lcd_4/` — Waveshare ESP32-S3-Touch-LCD-4 (ST7701 RGB parallel, 480×480 square, GT911 touch). Build env: `waveshare_lcd_4`. **RGB-panel port**: Arduino_ESP32RGBPanel + bounce buffers (tearing fix). IO expander @ 0x24 (TCA9554 / CH32V003) must init before `gfx->begin()` or the panel stays dark; backlight is expander pin 2 (on/off only). No AXP2101 / IMU; KEY/PWR is hardware RST. Single BOOT button (GPIO 0 → Space/PTT).

Plus one non-hardware target: `boards/sim/` — **native desktop simulator** (SDL2 window, 480×480, `platform = native`). Build env: `sim`. See "Desktop simulator" below.

**C6 ports have no PSRAM** — shared code gates on `BOARD_HAS_PSRAM` (absent on C6) to use `MALLOC_CAP_INTERNAL` for LVGL/splash buffers, and the `screenshot` serial command is disabled (`LV_USE_SNAPSHOT=0`), so UI changes on a C6 board must be eyeballed on hardware, not auto-captured.

The shared code calls a small HAL (`firmware/src/hal/`) that each board implements: display, touch, input, power, IMU. Optional features are guarded by `BoardCaps` (runtime) and `BOARD_HAS_*` (compile-time) rather than `#ifdef BOARD_*`.

Connects to a host daemon over BLE; daemon polls Anthropic API for usage data. This file is for future Claude Code sessions to bootstrap quickly. Read this first.

## Hardware (critical pins)

### AMOLED-2.16 (original)
- Display: **CO5300** AMOLED via QSPI (CS=12, SCLK=38, SDIO0..3=4..7, RST=2)
- Touch: **CST9220** via I2C (SDA=15, SCL=14, INT=11, addr=0x5A)
- PMU: **AXP2101** on same I2C bus (addr=0x34) — battery, USB VBUS, PWR button IRQ
- IMU: **QMI8658** on same I2C bus (addr=0x6B) — accelerometer for auto-rotation
- Buttons: GPIO 0 (left → Space/voice-mode), GPIO 18 (right → Shift+Tab/mode-toggle), AXP PKEY (middle → cycle screens; on splash → cycle animations)

### AMOLED-1.8 (newer port)
**Two hardware revisions ship under this name; the firmware probes I2C at boot and picks drivers automatically (`board_rev()`):**
- Display: **SH8601** (original) or **CO5300** (later rev) AMOLED via QSPI (CS=12, **SCLK=11** ← different!, SDIO0..3=4..7, RST routed via XCA9554 EXIO1). Both are `Arduino_OLED` subclasses held behind one base pointer in `display.cpp`. The CO5300's 368-wide active area starts at GRAM column 16, so it gets `CO5300_COL_OFFSET 16` to center; SH8601 needs none.
- Touch: **FT3168** @ 0x38 (original) or **CST816** @ 0x15 (later rev), via I2C (SDA=15, SCL=14, INT=21). Both expose the same FocalTech-style data layout at regs 0x02..0x06, so one inline reader in `touch.cpp` serves both — only the address differs. Avoids vendoring the GPLv3 `Arduino_DriveBus` library. Revision is detected by which touch address ACKs (CST816 present ⇒ CO5300 panel).
- PMU: AXP2101 @ 0x34 (same chip as 2.16 — `XPowersLib` reused; battery is an optional kit add-on but PMU + charging circuitry are populated)
- IMU: QMI8658 @ 0x6B (same chip — initialized for I2C bus health, rotation logic disabled)
- IO expander: **XCA9554 / PCA9554** @ I2C 0x20. Gates LCD_RST, TP_RST, audio amp enable, and reads the PWR button. **`io_expander_init()` MUST run before `gfx->begin()` or `ft3168_init()`** — otherwise display/touch stay in reset and silently fail. PWR button is on EXIO4, active HIGH (verified empirically with the deleted `iox` serial debug command).
- Orientation: **fixed at 0°**. IMU auto-rotation is disabled; `rotate_strip()` / `handle_rotation_change()` are excluded via `#ifndef BOARD_AMOLED_18`.
- Buttons: GPIO 0 (BOOT → Space/voice-mode), XCA9554 EXIO4 (PWR → cycle screens; on splash → cycle animations). **No third button** (GPIO 18 button doesn't exist on this board).

### AMOLED-1.8 (C6) — `waveshare_amoled_18_c6`
ESP32-C6 sibling of the S3 1.8: same 368×448 SH8601 panel + FocalTech touch, different SoC and GPIO map. **All pins/edges below verified on hardware via temporary GPIO/IRQ scans, since Waveshare's wiki publishes no pin table and the third-party BSP's numbers were partly wrong.**
- Display: **SH8601** AMOLED via QSPI (CS=5, SCLK=0, SDIO0..3=1..4, no MCU reset pin — internal POR; effective reset is the TCA9554 power-cycle). Stock `Arduino_SH8601` init (no vendor-register patch — that's only needed on the C6 2.16).
- Touch: **FT3168** (some units FT6146) @ I2C 0x38, INT=15. Same inline FocalTech reader as the S3 1.8 (regs 0x02..0x06); no reset pin (gated by TCA9554 touch power).
- I2C bus: SDA=8, SCL=7 (shared by TCA9554, AXP2101, FT3168, QMI8658, PCF85063 RTC, ES8311 codec).
- IO expander: **TCA9554 / PCA9554** @ 0x20 — here it gates **power**, not reset: **P4 = display power, P5 = touch power, P7 = audio amp**. `io_expander_init()` runs the documented power-on sequence (P4/P5 LOW → 200 ms → HIGH) and **MUST run before `display_hal_init()`** or the panel stays unpowered. Amp (P7) left off (no audio path).
- PMU: AXP2101 @ 0x34 (owned by `power.cpp`, not `board_init` — LCD isn't on an ALDO rail here).
- IMU: QMI8658 @ 0x6B (init'd for bus health, rotation disabled).
- Orientation: **fixed at 0°**, no rotation (no PSRAM headroom).
- Buttons: **GPIO 9** (BOOT → Space/voice-mode, active LOW — *not* the docs' GPIO 0/9 guess; confirmed by scan), **AXP2101 PKEY** (PWR → cycle screens; on splash → cycle animations). The PKEY **SHORT-press IRQ fires on release** — that's the edge `power.cpp` acts on. No secondary button.

### AMOLED-2.06 (watch form factor) — `waveshare_amoled_206`
- Display: **CO5300** AMOLED via QSPI (CS=12, **SCLK=11** ← same as 1.8, SDIO0..3=4..7, RST=8 direct GPIO). 410×502 portrait. Requires **`col_offset1 = 23`** in the `Arduino_CO5300` constructor — the panel's visible viewport sits at a 22–23 column offset inside the controller's internal RAM. Without it, a vertical strip of stale/garbage content shows through on the right edge (23 was picked empirically for centering; Waveshare's reference library uses 22). The 2.16 dodges this because its 480×480 viewport fills the controller's RAM.
- Touch: **FT3168** via I2C (SDA=15, SCL=14, **INT=38, RST=9** direct GPIO, addr=0x38). Same inline FocalTech reader as the 1.8 port (no GPLv3 `Arduino_DriveBus` dependency). Coordinates verified end-to-end with the BLE reset zone.
- PMU: AXP2101 @ 0x34 (same chip as 2.16/1.8 — `XPowersLib` reused). PWR button routes through AXP PKEY IRQs (short / long / positive), same path as the 2.16 — no IO expander.
- IMU: QMI8658 @ 0x6B (initialized for I2C bus health; rotation logic disabled — fixed watch enclosure orientation).
- RTC: **PCF85063** on the same I2C bus, powered through AXP2101 for retention. Not used by Clawdmeter but present for future features.
- Audio codec: **ES8311** + ES7210 ADC on the same I2C bus. The amp path is unverified on this board, so `sound.cpp` no-ops (same posture as the C6 1.8) — the shared `chime.cpp` engine is ready to wire up once it's tested on hardware.
- **No IO expander** despite the Waveshare wiki FAQ implying one. The schematic shows Key3/PWR wired directly to AXP2101 PWRON; touch reset and display reset are direct GPIOs. `board_init()` pulses LCD_RESET (GPIO 8) and TP_RESET (GPIO 9) before display/touch HAL init.
- Buttons: GPIO 0 (BOOT → Space/voice-mode), AXP PKEY (PWR → cycle screens; hold-to-pair). **No third button**.
- Flash: 32 MB. Uses `default_32MB.csv` partition table.

### LCD-1.54 (TFT) — `waveshare_lcd_154`
Pin map cross-checked against Waveshare's own XiaoZhi board config (`main/boards/waveshare/esp32-s3-touch-lcd-1.54/config.h` in 78/xiaozhi-esp32) — the factory firmware shipped on the unit. Module is ESP32-S3R8: 16 MB quad flash + 8 MB embedded octal PSRAM.
- Display: **ST7789** TFT via 4-wire SPI (CS=21, SCLK=38, MOSI=39, DC=45, RST=40), 240×240, colour inversion on. **Backlight on GPIO 46 via LEDC PWM** — a TFT has no in-panel brightness command, so `display_hal_set_brightness()` is a PWM duty. **Turned a quarter turn left** — `LCD_ROTATION_LEFT` in `board.h` picks GFX rotation 3 (MADCTL MY|MV, free) and `touch.cpp` turns its coordinates back. Rotation 3 reverses the page order, so the 240-row window sits at the far end of the 240×320 GRAM: `row_offset2 = 80` (rotation 0 needs no offsets).
- Touch: **CST816** @ 0x15 (SDA=42, SCL=41, INT=48, RST=47). Same FocalTech-style inline reader as the 1.8 port.
- **No PMU.** Battery % from a 3:1 VBAT divider on GPIO 1 (ADC). **GPIO 2 = BAT_EN power-hold latch** — `board_init()` must drive it HIGH or the board dies on battery; `power.cpp` drops it for the 8 s power-off. **GPIO 3 = charger status, LOW while charging.** VBUS itself is not sensed, so `power_hal_is_vbus_in()` stays false.
- Audio: ES8311 @ 0x18 (I2S MCLK=8, BCLK=9, WS=10, DOUT=12), amp enable GPIO 7. ES7210 mic ADC present, unused.
- IMU QMI8658 and RTC PCF85063 present, unused. Orientation fixed (no auto-rotation) — see the quarter turn left above.
- Buttons (labels printed on the case): **BOOT = GPIO 0** (Space), **PLUS = GPIO 4** (Shift+Tab), **PWR = GPIO 5** (cycle screens, hold-to-pair, 8 s = power off). All plain active-LOW GPIOs; PWR edges are synthesized in software in `power.cpp`.

### Knob-1.8 (round TFT) — `waveshare_knob_18`
Pins from Waveshare's demo package (`08_LVGL_Test/lcd_config.h`, `04_Encoder_Test`, `03_DRV2605_Test`) checked against the schematic. ESP32-S3R8, 16 MB flash. A second MCU (ESP32-U4WDH) owns classic-BT audio and the second ring encoder; the port never talks to it.
- Display: **ST77916** via QSPI (CS=14, SCLK=13, SDIO0..3=15..18, RST=21), 360×360, only the inscribed circle visible. **Neither of Arduino_GFX's two ST77916 init tables fits this panel** — `display.cpp` carries Waveshare's 181-command vendor table in GFX batch-op form, plus COLMOD 0x55 (esp_lcd set it implicitly). 40 MHz, even-aligned flush regions. **Backlight = LEDC PWM on GPIO 47.** **Mounted 180° (USB-C at the top)** — `LCD_ROTATION_180` in `board.h` sets GFX rotation 2 (MADCTL MX|MY, free) and `touch.cpp` mirrors both axes to match.
- Touch: **CST816** @ 0x15 (SDA=11, SCL=12, INT=9, RST=10), same inline reader as the LCD-1.54.
- **Ring = bidirectional detent switch, not a quadrature encoder**: GPIO 8 pulses LOW once per detent one way, GPIO 7 the other way. Sampled every 3 ms on an esp_timer (Waveshare's `bidi_switch_knob.c` logic); `input_hal_encoder_steps()` hands the count to `main.cpp`, which maps it to the PWR short press in both directions (next/prev animation, brighter/darker).
- Haptics: **DRV2605L** @ 0x5A on the touch bus, ERM open loop + ROM library 1 as in the demo; one click per `input_hal_encoder_steps()` call that returns a turn.
- **No reachable keys.** BOOT (GPIO 0) is on the PCB but inside the closed case, and there is no PWR key. `touch_keys` moves their jobs to the screen: **tap** = toggle screens (after a 300 ms double-tap window), **double tap** = Shift+Tab, **hold** = Space while a host is connected (voice-mode PTT), **hold 3–6 s + release while disconnected** = pair. BOOT still works as Space / hold-to-pair with the case open.
- **No battery gauge**: `BATT_ADC` (GPIO 1) divides the 5 V rail, not the cell. **No chime**: the PCM5100A DAC only has a line-out on the connector and its XSMT mute is driven by the second MCU.

### LCD-4 — `waveshare_lcd_4`
- Display: **ST7701** 480×480 RGB parallel (DE=40, VSYNC=39, HSYNC=38, PCLK=41, R0-4=46/3/8/18/17, G0-5=14/13/12/11/10/9, B0-4=5/45/48/47/21); ST7701 init via SW SPI (CS=42, SCK=2, MOSI=1).
- Touch: **GT911** via I2C (SDA=15, SCL=7), polled (wiki INT=GPIO 16 unused). Probe 0x5D then 0x14.
- IO expander: **addr 0x24** (fallback 0x20) on the same I2C bus — must init before `gfx->begin()` (output 0xFF, config 0x3A). Backlight is expander pin 2.
- No PMU / IMU. Buttons: GPIO 0 only (BOOT → Space/PTT). KEY/PWR is EN/RST (hardware reset). GPIO 18 is display R3.
- RGB tearing fix: pass `bounce_buffer_size_px = LCD_WIDTH * 10` to `Arduino_ESP32RGBPanel`. Do not call `rgbpanel->getFrameBuffer()` after `gfx->begin()`.

## Architecture

```text
firmware/src/
  hal/                      — board-agnostic interfaces shared code calls into
    board_caps.h            — runtime BoardCaps struct (W, H, button_count, has_* flags)
    display_hal.h           — init / begin / set_brightness / draw_bitmap / tick / round_area
    touch_hal.h             — init / read(&x, &y, &pressed)
    input_hal.h             — init / is_held(PRIMARY|SECONDARY)
    power_hal.h             — init / tick / battery_pct / is_charging / pwr_pressed (edge)
    imu_hal.h               — init / tick / rotation_quadrant
  boards/
    waveshare_amoled_216/   — CO5300 + CST9220 + AXP PKEY + QMI8658 rotation
    waveshare_amoled_18/    — SH8601 + FT3168 + AXP + XCA9554 (PWR via EXIO4), no rotation
    waveshare_amoled_216_c6/— C6: SH8601 + CST9217 + AXP PKEY, no PSRAM
    waveshare_amoled_18_c6/ — C6: SH8601 + FT3168 + AXP PKEY + TCA9554 (gates power), no PSRAM
    waveshare_amoled_206/   — CO5300 + FT3168 + AXP PKEY, no IO expander, 32 MB, no rotation
    waveshare_lcd_154/      — ST7789 SPI TFT + CST816 + GPIO buttons, no PMU (ADC battery), 240×240
    waveshare_knob_18/      — round ST77916 QSPI TFT + CST816 + rotary ring + DRV2605, 360×360
    template/               — copy this to bootstrap a new port
  main.cpp                  — setup() + loop(): HAL calls only, zero #ifdef BOARD_*
  ui.{h,cpp}                — 3-screen UI (splash, usage, bluetooth). compute_layout() picks fonts/positions from board_caps() (responsive — breakpoints: H >= 460 → large, H >= 300 → compact, else small)
  splash.{h,cpp}            — 20×20 pixel-art engine. CELL = min(W,H)/20, centered.
  ble.{h,cpp}               — NimBLE peripheral: custom data service + HID keyboard
  data.h                    — UsageData struct
  icons.h                   — icon arrays. Battery (5×) are RGB565A8 with alpha; rest are raw RGB565.
  logo.h                    — 80×80 RGB565 logo
  font_*.c                  — pre-compiled LVGL 9 bitmap fonts (Tiempos 56/34, Styrene 48/28/24/20/16/14/12, Mono 32/18)
  splash_animations.h       — generated, do not hand-edit
docs/porting/               — adding-a-board.md, hal-contract.md, capability-flags.md
```

Each board folder contains: `board.h` (pins, I2C addresses, `BOARD_HAS_*` flags),
`board_init.cpp` (Wire.begin + any IO expander), `display.cpp`, `touch.cpp`,
`input.cpp`, `power.cpp`, `imu.cpp`, `caps.cpp` (the `BoardCaps` instance), plus
any board-private hardware drivers (e.g. `io_expander.{h,cpp}` on AMOLED-1.8).
PlatformIO's `build_src_filter` includes shared code + one board's folder per env.

## Build / flash

```bash
pio run -d firmware -e waveshare_amoled_216                                     # build 2.16 (S3, default original)
pio run -d firmware -e waveshare_amoled_18                                      # build 1.8 (S3)
pio run -d firmware -e waveshare_amoled_216_c6                                  # build 2.16 (C6)
pio run -d firmware -e waveshare_amoled_18_c6                                   # build 1.8 (C6)
pio run -d firmware -e waveshare_amoled_206                                     # build 2.06 (S3, watch)
pio run -d firmware -e waveshare_lcd_154                                        # build LCD-1.54 (S3, TFT)
pio run -d firmware -e waveshare_knob_18                                        # build Knob-1.8 (S3, round TFT)
pio run -d firmware -e waveshare_lcd_4                                           # build LCD-4 (S3, RGB TFT)
pio run -d firmware -e waveshare_amoled_18 -t upload --upload-port /dev/cu.usbmodem101   # flash 1.8 on macOS
pio run -d firmware -e waveshare_amoled_216 -t upload --upload-port /dev/ttyACM0         # flash 2.16 on Linux
# C6 boards: same native USB-JTAG flashing; flag a chip mismatch ("This chip is ESP32-C6,
# not ESP32-S3") means you picked an S3 env — use a *_c6 env for C6 hardware.
```

If `pio` isn't on PATH: try `~/.platformio/penv/bin/pio` (Linux/macOS pio install) or `brew install platformio` on macOS.

Device path differs by OS: `/dev/cu.usbmodem*` on macOS, `/dev/ttyACM0` on Linux. Both expose the ESP32-S3 native USB-JTAG (no boot-mode dance needed).

**Windows:** the board shows up as `USB JTAG/serial debug unit` + `COMn` (VID 303A). pio lives at `%USERPROFILE%\.platformio\penv\Scripts\pio.exe` if installed into that venv. The first build after a fresh platform install can die with `Failed to install Python dependencies` — pioarduino reinstalls its `platformio` into the running `pio.exe`'s venv, which Windows won't allow; pre-install `python_deps` from `platforms/espressif32/builder/penv_setup.py` with the penv's `uv.exe pip install` (README → Windows → Flash). **Don't chain `-t erase -t upload`** in one `pio run`: the rebuild it triggers failed with `SPI.h`/`Wire.h: No such file`; run them as two commands. Unknown board? Waveshare's factory firmware logs its board name at boot (e.g. `esp32_s3_touch_lcd_1_54: SPI BUS init`), and `esptool flash-id` gives chip/PSRAM/flash size.

## Desktop simulator (`-e sim`) — develop UI without hardware

```bash
sudo apt install libsdl2-dev   # once (macOS: brew install sdl2)
pio run -d firmware -e sim && (cd firmware && .pio/build/sim/program)
```

An SDL2 window stands in for the 480×480 panel; the **full firmware loop runs
unmodified** — `main.cpp`, `ui.cpp`, `splash.cpp`, idle fade, pair gesture,
JSON parsing, usage-rate/chime logic. Only `ble.cpp`/`chime.cpp` are swapped
for stubs. How it works: `boards/sim/` implements the HAL against SDL2, thin
Arduino shims live in `boards/sim/shim/` (`millis`/`Serial`→stdio,
`heap_caps`→malloc, in-memory `Preferences`), and `ble_sim.cpp` plays back
daemon payloads from `firmware/sim/scenario.jsonl` (one JSON line per state +
optional `name`/`hold_ms`; override with `SIM_SCENARIO=<path>`).

Controls (full map in `boards/sim/board.h`): mouse = touch · space =
play/pause scenario · ←/→ = step · 1-9 = jump · d = BLE link toggle ·
b/n = BOOT/secondary buttons · p = PWR · c/-/= = charging/battery ·
s = screenshot BMP · esc = quit.

Headless screenshots (works in CI, no display):
`SDL_VIDEODRIVER=dummy SIM_AUTOSHOT_MS=6000 .pio/build/sim/program` saves
`sim-autoshot.bmp` (or `SIM_AUTOSHOT_PATH`) after 6 s and exits. Combine with
the boot-screen swap trick below to capture any screen. **The sim renders with
desktop LVGL and fake data — always do a final check on real hardware before
merging panel-related changes** (col offsets, rotation, rounding live in the
hardware boards, not shared code).

## QA your own UI changes — don't ask the user

The firmware ships a `screenshot` serial command that dumps the LVGL framebuffer. `./screenshot.sh out.png [port]` captures a PNG sized to the active display (480×480 or 368×448). **Use this on every UI iteration** — Read the PNG with the Read tool, verify the change visually, iterate. Script auto-picks the macOS/Linux default port and falls back to pio's bundled Python if pyserial isn't on the system Python.

The boot screen is `SCREEN_SPLASH` and only advances on a physical button press, so a fresh flash will sit on the splash. To screenshot the screen you're actually editing without asking the user to press a button, **temporarily change the default boot screen** in `main.cpp` (search for `ui_show_screen(SCREEN_SPLASH);`) to `SCREEN_USAGE` / `SCREEN_CONTROLLER` / `SCREEN_BLUETOOTH`, do your iteration, then revert before committing.

## Critical gotchas

1. **CO5300 cannot rotate.** Its MADCTL only supports axis flips, not column/row exchange. Rotation is done by **CPU pixel remapping inside `display_hal_draw_bitmap`** in `boards/waveshare_amoled_216/display.cpp`. We use **PARTIAL render mode with strip rotation** (small 480×40 strips, fast). On rotation change → AMOLED brightness flash → force redraw (handled inside `display_hal_tick`).
2. **OPI PSRAM** required: `board_build.arduino.memory_type = qio_opi` in platformio.ini. Without this, `MALLOC_CAP_SPIRAM` returns NULL and the screen is black.
3. **pioarduino platform required.** GFX Library for Arduino needs Arduino Core 3.x (`esp32-hal-periman.h`), not the 2.x that standard `espressif32` ships. We pin `pioarduino/platform-espressif32` 55.03.38-1.
4. **LVGL 9 font patching.** `lv_font_conv` outputs LVGL 8 format. Must remove `#if LVGL_VERSION_MAJOR >= 8` guards, drop `.cache` field, add `.release_glyph`, `.kerning`, `.static_bitmap`, `.fallback`, `.user_data`. Without patching, fonts render invisible.
5. **Touch reading is centralized inside each board's `touch.cpp`.** The HAL `touch_hal_read()` is called once per loop from `my_touch_cb`; the board's implementation owns its latched `touch_pressed/x/y` state. Don't call the underlying controller from anywhere else — CST9220's `getPoint()` etc. do a full I2C transaction and concurrent callers consume each other's data.
6. **Even-aligned flush regions.** `display_hal_round_area` (called from `rounder_cb`) is what each board uses to enforce this. Required on CO5300, harmless on SH8601.
7. **Touch axis swap/mirror is per-board.** The 2.16's CST9220 needs `setSwapXY(true)` + `setMirrorXY(true, false)` — applied inside `boards/waveshare_amoled_216/touch.cpp::touch_hal_init()`. New ports apply their own.
8. **LVGL RGB565A8 is planar.** `w*h` RGB565 pixels followed by `w*h` alpha bytes; `data_size = w*h*3`, `stride = w*2`. Use `init_icon_dsc_rgb565a8()` for icons that overlap non-uniform backgrounds (e.g. battery over splash). Lucide source PNGs are black-on-transparent — converter must tint to white or icons render invisible. See `tools/png_to_lvgl.js`.
9. **Per-board pre-init is `board_init()`.** Each board's `board_init.cpp` brings up `Wire` and any reset-gating IO expander BEFORE `display_hal_init()`. Skipping the IO expander release on AMOLED-1.8 leaves SH8601 + FT3168 in reset and they silently fail to probe.
10. **No `#ifdef BOARD_*` in shared code.** The whole point of the refactor — if you're about to add one, you probably want a `BoardCaps` field or a per-board file instead. See `docs/porting/capability-flags.md`.
11. **LCD-4 RGB bounce buffers.** `Arduino_RGB_Display` DMA-scans PSRAM. Pass `bounce_buffer_size_px = LCD_WIDTH * 10` so ESP-IDF allocates SRAM bounce buffers. Do not call `rgbpanel->getFrameBuffer()` after `gfx->begin()` — it constructs a second RGB panel and crashes.
12. **LCD-4 has only one user button (GPIO 0 / BOOT).** GPIO 18 is display R3. KEY/PWR is EN/RST (hardware reset). Hold-to-pair and PWR-short animation/brightness cycling are unavailable; tap the panel to toggle splash ↔ usage.

## Icons

`tools/png_to_lvgl.js <input.png> <symbol> [W_MACRO] [H_MACRO] [--tint=RRGGBB | --no-tint]` converts an alpha PNG to RGB565A8. Default tint is white (`0xFFFFFF`) — necessary for Lucide PNGs. Splice output into `firmware/src/icons.h` and use `init_icon_dsc_rgb565a8()` in ui.cpp. Currently only the 5 battery icons use this format; the rest are still raw RGB565 baked over the panel background, fine because they live inside opaque zones.

## Splash animations

17 official Anthropic Clawd animations (core poses + persona scenes), archived
with full provenance in `research/clawd-official/`. Pipeline:

```bash
node tools/convert_official_clawd.js            # → firmware/src/splash_animations.h
node tools/convert_official_clawd.js --verify DIR   # + per-animation PNGs for eyeballing
```

Requires ImageMagick; Laptop and Soccer convert from their Lottie exports
(crisp) rather than GIFs. Frames are bounding-box crops on the official 55×37
art stage (ox/oy = stage offset — every animation shares one idle-Clawd
position, so transitions are seamless), one byte per cell into a per-animation
≤16-color RGB565 palette (index 0 = background, true black), per-frame hold ms
with duplicates collapsed (~400 KB total). The converter also: detects each
animation's **loop region** (gait cycles, scene middles; sailing scene's is
located by cross-matching the standalone sailing-loop asset, which is not
emitted), synthesizes the **eyes** (transparent holes in the source GIFs) as
`#141413` ink via border flood-fill, and applies two contrast recolors
(trumpet notes → ivory, magnifier fedora → gray) via component analysis.

The splash engine (`splash.cpp`) plays intro → loop → outro on a **60×60
stage** (`SPLASH_GRID`, cell = min(W,H)/60 → 8 px on 480, 6 px on 368, 4 px on
240): loops hold until released (walk arrival, scene timer, rotation), so
switches always pass through the shared idle pose. Walkers translate with
foot-locked per-frame schedules and mirror when heading left. Usage-rate
groups pick animations by name; the same rate drives the **corner mascot** on
the usage screen (`splash_mascot_*`, PSRAM boards; C6 falls back to the static
`clawd_still.h` icon) — idle stills, rate-scaled acts, and walk-off/lurk/
walk-back trips. Default boot screen.

**Where the animations come from / finding new ones:** all assets are plain
files under `https://claude.ai/images/clawd/{core,persona}/…` — static assets
are not Cloudflare-gated, only HTML routes are. The asset server returns a
real GIF for a valid filename and an HTML catch-all (both HTTP 200) otherwise,
so **name probing works**: fetch `Clawd-<Name>.gif` and check the magic bytes.
Seven current animations are referenced by no shipped bundle and were found
exactly this way (Anthropic stages seasonal drops — Soccer appeared for the
World Cup). To hunt for new ones: run `research/clawd-official/fetch.sh`
(extend its probe list), and grep a fresh desktop .deb's `ion-dist/` bundles
for `/images/` paths (`research/clawd-official/CLAUDE.md` documents the full
methodology, including the Lottie sources and the assets-proxy).


**This fork's additions:** `research/clawd-official/Clawd-Book.gif` ("book",
found 2026-10-01 by name probing) and the two stills `still` / `cloud still`
(single-frame, from the PNGs) are converted too. Host state names in the BLE
`a` field ("work coding", "allow", "done", … — what `daemon/clawd_activity.py`
and the Windows Session Browser send) map onto official animations through
`HOST_ALIASES` in `splash.cpp`; idle names hand back to the rate groups. The
footer in `ui.cpp` matches the raw host names, so it is independent of the art.
The corner mascot plays the host state in place when `ua` is set
(`splash_mascot_set_host_anim`). No ImageMagick on this Mac: a PIL stand-in for
`convert`/`identify` reproduces upstream's header byte-for-byte.

## User profile / preferences

See `~/.claude/projects/.../memory/` files for persistent context (user is an embedded-beginner senior dev, brand-conscious, prefers iterative UI refinement, dislikes me authoring my own art when third-party assets are intended). Always read those memory files at session start.

## Recent session highlights

- **Windows daemon mirrors Claude Code's state; "Needs you" no longer lags (2026-10-04).** The Windows daemon used to send usage only; it now reads the same hook state files as the macOS one (logic ported 1:1, see "Claude Code hooks" below). Logging every hook event on Windows showed the desktop app sends the `permission_prompt` Notification ~6 s after a permission dialog or AskUserQuestion opens, so the device sat on busy Clawd while Claude was already waiting; `PermissionRequest` fires as the dialog opens and now drives `allow`. Verified on two LCD-1.54s (`Clawdmeter A9E9`, `B4E9`) with a Windows 11 PC. Also: the Windows tests used to append their mocked outages to the *running* daemon's `daemon.log` — `daemon/tests/conftest.py` switches the file logger off.
- **The board could stop advertising forever (2026-09-12).** Symptom: pairing only worked if the host was started at the same instant as the pairing gesture; wait a few seconds and the board was gone from the host's list. Cause: advertising ends on this hardware without any connect/disconnect of ours (OSes probe HID advertisers and drop them, and a connection that dies during establishment fires no `onConnect`), while the only restarts were `onDisconnect` and `ble_clear_bonds()`. Miss those and the board sat in `state=ADVERTISING, ble_gap_adv_active()=false` indefinitely — logging "advertising start=OK" the whole time. `ble_tick()` now re-checks every 2 s and restarts whenever a connection slot is free (free slot, not zero connections — the OS holds the HID link while the daemon needs the second one). Verified via Windows' `BluetoothLEDevice` enumeration: absent from three consecutive scans before, present in every scan after, and pairing then succeeded first try. **Debugging note:** `NimBLEAdvertising::start()` returning true proves nothing — `isAdvertising()` (`ble_gap_adv_active()`) is the honest one.
- **The pairing hint says when a host is the problem (2026-09-12).** A host that still holds a bond the board no longer has keeps listing the board as paired and keeps failing to connect — Windows says "gekoppelt", the board says "To pair", and nothing reconciles the two. The board does know: `onAuthenticationComplete` sees `bonded=0 enc=0`. `ble_pairing_rejected()` reports that for three minutes after the last failed handshake (cleared by a successful bond and by `ble_clear_bonds()`), and the hint becomes "Pairing failed / the host has a stale key / remove it there, then retry". Diagnosing this on hardware: the serial log shows the failed handshake, and an NVS dump (`esptool read-flash 0x9000 0x5000`) shows whether `clawd/owner` and the `nimble_bond` keys actually exist — on the Knob-1.8 they did not, confirming the board had no bond while the host thought it did.
- **Hold-to-pair now reports its state (2026-09-12).** The gesture used to be blind — `pair_tick()` only wrote to the serial console, so a 3-second hold with a 6-second cut-off had to be timed by feel, and a successful pair looked exactly like no pair at all. `ui_set_pair_state()` (ui.h) drives an overlay that floats above both the usage view and the splash: "Keep holding" → **"Release now to pair"** → "Release and retry" past the window → "Ready to connect" once bonds are cleared (self-clears after 2 s). The splash paints straight to the panel on the PSRAM-less boards, so it consults `ui_pair_overlay_active()` the same way it already consults `charge_anim_is_active()`. The sound HAL gained `sound_hal_play_pair_armed()` / `sound_hal_play_paired()` — synthesized beeps via the new `chime_play_cue()` (no extra PCM in flash) on the ES8311 boards, no-ops elsewhere. **The Knob-1.8's DRV2605 was tried and dropped:** the driver is healthy (DEVICE_ID 7, auto-calibration passes, real back-EMF measured) but nothing it plays is perceptible — every ROM effect and constant full-amplitude RTP drive, at both the stock ~3 V clamp and the 5.4 V maximum, went unnoticed on hardware. The motor is too small for the knob's mass. Pairing feedback there is the overlay alone; the ring's detent click stays. Touch-driven pairing (Knob) gets the same feedback through a new nullable `UiTouchKeys.hold_tick`, fed by LVGL's `LONG_PRESSED_REPEAT`.
- **AMOLED-1.8 chime verified on hardware + EXIO2 touch-kill fix (2026-07-13).** The 1.8's `amp_enable` hook drove both GPIO 46 and XCA9554 EXIO2 ("the unused one is harmless") — but pulling EXIO2 low takes the FT3168 off the I2C bus (chip stops ACKing; IDF reports it as `ESP_ERR_INVALID_STATE`, which reads like a driver wedge and cost a long I2S red-herring chase). Amp enable is GPIO 46 only; EXIO2 must stay HIGH. Chime, touch, buttons, and BLE bond persistence all verified on a real 1.8.
- **Device-abstraction refactor (2026-05-18).** All board-conditional code moved out of shared files into `boards/<name>/` and behind a HAL in `hal/`. ~30 `#ifdef BOARD_*` blocks went to zero. UI is responsive via `compute_layout()` driven by `board_caps()`. New ports add a folder + a PlatformIO env — no shared file edits.
- Added second board port: Waveshare AMOLED-1.8 (368×448 portrait, SH8601, FT3168, XCA9554 IO expander).
- Migrated from Panlee SC01 Plus (480×320 IPS) to Waveshare 2.16" AMOLED (480×480 square). Full hardware/library swap.
- Added IMU auto-rotation, battery indicator, USB-state-aware screen switching.
- Added splash screen with scraped pixel-art animations and 3-button physical input layout.
- Fonts and icons re-scaled ~1.9× for the higher-DPI panel.
- All UI margins widened to 20px to clear the rounded display corners.
- Battery icons converted to RGB565A8 alpha so they blend cleanly over the splash animations.

## Daemon / host side

Bash daemon (`daemon/claude-usage-daemon.sh`) reads OAuth token, polls Anthropic API, sends JSON over BLE GATT. Run with `systemctl --user start claude-usage-daemon`. The unit file's `ExecStart` is the absolute path to the script — repoint it when switching between the worktree and the main checkout.

**Discovery & resilience:**

- Connects by name (`"Clawdmeter"`) on first run, caches resolved MAC at `~/.config/claude-usage-monitor/ble-address`. ESP32 BLE addresses are factory-burned per-chip, so swapping any board invalidates the cache.
- **Boards advertise `Clawdmeter <last 2 MAC bytes>`** (e.g. `Clawdmeter 35F9`), set in `ble_init()` — without it every board announced the same name and a daemon bonded to one would connect to another and drop it in a loop, which is exactly what was seen with two boards on the desk. Name matching is therefore a **prefix** match everywhere: `startswith` in the macOS path, `-like 'Clawdmeter*'` in the Windows PnP query, `grep` in the bash daemon. Don't tighten any of them back to equality.
- **Several boards paired with one host** (Windows): `discover_bonded_addresses()` returns *every* match and `acquire_target()` walks them, advancing on each failed attempt and staying put after a successful one. It used to return the first PnP row, which let one powered-off board retry forever while a reachable one sat beside it — two hours of `TimeoutError` in the field. A `device = F629` line in the config sorts the preferred board first without hiding the others, so a typo degrades to "wrong order", not "no device". **`CLAWDMETER_BLE_ADDRESS` beats all of it** — a stale value there is invisible from the log's point of view and was the actual cause of that field incident.
- On connect failure: cache is dropped AND device is removed from bluez (`bluetoothctl remove`) so the next scan won't re-pick a dead MAC. Multi-candidate scans pick `head -1` and let the failure cycle converge.
- `POLL_INTERVAL=60`, `TICK=5`. Inner loop wakes every 5s to detect disconnects fast; polls Anthropic when 60s elapsed OR when ESP fires a refresh request.

**Claude Code hooks (macOS + Windows daemons):**

- `daemon/clawd_activity.py --install` adds entries to `~/.claude/settings.json` for `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `PermissionRequest`, `Notification`, `Stop`, `SessionEnd`; only commands containing `clawd_activity.py … --hook` are ever touched. Each event drops one state file per session; the daemon folds them every second into the payload's `a` (priority: limit > allow > write > work > done > idle > sleep) and resends between polls. **Adding an event means existing installs must re-run `--install`** — `is_installed()` only checks that some hook of ours exists.
- Windows specifics: the hook command runs the **base** interpreter's `pythonw.exe` (no console flash per tool call; the venv's pythonw is a stub that relaunches console python) with forward-slash paths (same meaning in cmd.exe and Git Bash); state files go to `~/.config/claude-usage-monitor/activity` — **never under `%LOCALAPPDATA%`**, see the MSIX note below; `settings.json` is read/written as UTF-8 explicitly (the locale code page would mangle it). Hook changes in `settings.json` take effect in running desktop-app sessions without a restart.
- **Windows + Claude desktop app = MSIX container.** Everything the desktop app starts — Claude Code, its hooks, and every shell command a Claude session runs — gets its `AppData` *and* `HKCU` writes redirected into `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\…` / the package's private registry hive, and reads see that merged view. Consequences seen 2026-10-08: `install-windows.ps1` run from a session wrote the `Run` value only into the package hive (Explorer skipped it at logon — nothing in the real HKCU); the tray started from a session wrote its log into the package's copy of `daemon.log` and died when the session ended; hook state files under `%LOCALAPPDATA%` never reached the real daemon. `~` (`.claude`, `.config`) and other drives are **not** redirected. To see the real files/registry from a session, run the command outside the container: `Invoke-CimMethod Win32_Process Create` (WMI-created processes are outside it) with output to a non-AppData path, e.g. the repo's `.venv\`. That is also how to (re)start the tray from a session so it outlives it — or ask the user to start it (Win+R, the autostart command without quotes; the paths have no spaces).
- Debugging which events fire: add a second, logging-only hook command next to ours (it's left alone by `--install`), reproduce, then remove it.

**GATT characteristics on service `4c41555a-...0001`:**

- `...0002` RX — daemon writes JSON usage payload here.
- `...0003` TX — firmware notifies ack/nack (daemon doesn't subscribe).
- `...0004` REQ — firmware fires `0x01` notify in `onSubscribe` if `has_received_data` is false. Daemon subscribes via `setsid bash -c "stdbuf -oL dbus-monitor … | awk …"`; awk drops a flag file the inner loop picks up. See the `feedback_dbus_monitor_pipe` memory for the three subtle gotchas (pipe buffering, busctl-exits race, `wait` blocking on pipeline jobs).

# meater-server

Reads MEATER Bluetooth cooking probes on Linux and gives you three things:

- a **web UI** you can open from a phone at the grill — live temperature, what
  is being cooked, and how long is left,
- an **ESPHome native API** endpoint, so Home Assistant discovers the probes as
  an ordinary ESPHome device,
- a **time-remaining estimate** that models how meat actually heats, instead of
  projecting the current rate in a straight line.

No cloud account, no MEATER app, no ESP32 in the middle. The probe talks
Bluetooth to this machine and nothing leaves your network.

Tested against an original MEATER and a MEATER+.

## Install

Needs Python 3.11+, BlueZ, and a Bluetooth adapter.

```sh
git clone <this repo> && cd meater-server
python3 -m venv .venv
.venv/bin/pip install -e .
```

## Quick start

Take a probe out of its charging block — a MEATER sleeps in the block and will
not answer — then:

```sh
.venv/bin/meater-server scan      # confirm the probe is visible
.venv/bin/meater-server -v run    # start the server
```

`run` prints every address the UI is reachable on. Open it from your laptop,
or from your phone on the same wifi.

### Viewing it from a phone

The server binds `0.0.0.0` by default, so the only thing usually in the way is
a host firewall. With `ufw`:

```sh
sudo ufw allow 8080/tcp comment 'meater-server web UI'
sudo ufw allow 6053/tcp comment 'meater-server ESPHome API'   # only if HA is on another machine
```

Then browse to `http://<this-machine>:8080/`. On iOS and macOS the mDNS name
works too (`http://<hostname>.local:8080/`); Android is less reliable, so use
the IP address there.

The page is installable: **Add to Home Screen** gives you a full-screen app
with its own icon. While a cook is running it holds a screen wake lock, so you
can stand the phone against the grill without it sleeping, and it fires a
notification when the probe hits its pull temperature.

## Configuration

Config is optional. The server looks for `./meater.toml`, then
`~/.config/meater-server/config.toml`, then `/etc/meater-server/config.toml`.
See [`meater.toml.example`](meater.toml.example) for every option; any field
can also be set as `MEATER_<FIELD>` in the environment.

By default the server binds whichever probes are awake and in range. Pin
specific probes only if you want a particular one always in slot 1:

```toml
[bluetooth]
addresses = ["AA:BB:CC:DD:EE:FF"]
```

A pinned slot waits for that probe forever. An unpinned slot that keeps not
answering is handed to a probe that *is* present, so one sleeping probe never
blocks a working one.

## Home Assistant

Start the server, then add the **ESPHome** integration and point it at this
machine on port 6053. mDNS discovery usually finds it on its own.

Encryption is off by default. To turn it on:

```sh
.venv/bin/meater-server keygen    # paste into config and into Home Assistant
```

```toml
[esphome]
encryption_key = "the key you generated"
```

Each probe slot gets:

| Entity | Notes |
|---|---|
| `tip_temperature`, `ambient_temperature` | °C, the two values the probe reports |
| `target_temperature`, `pull_temperature` | pull is the target minus carryover |
| `time_remaining`, `time_until_served` | seconds; the second includes the rest |
| `cook_progress`, `temperature_rate` | % toward the pull temperature, and °C/min |
| `battery`, `signal_strength`, `connected`, `address` | diagnostics |
| `cook`, `state` | what is cooking, and `idle`/`cooking`/`ready`/`resting`/`done` |
| `cook_preset` | **select** — choose the cook from Home Assistant |
| `target_override` | **number** — override the target temperature |
| `start_resting`, `clear_cook` | **buttons** |

Because `cook_preset` is a select, a Home Assistant automation can start a
cook, and `state` turning `ready` is a clean trigger for an alert.

The entity list is fixed at startup, since Home Assistant reads it once per
connection. If you want a second probe, set `slots = 2` and restart.

## How the time estimate works

Meat in a closed grill follows Newton's law of cooling: the centre approaches
the ambient temperature asymptotically, so the last few degrees take far longer
than the first few. Projecting the current rate forward is badly optimistic
exactly where you care — on a brisket it can say four hours when the real
answer is six and a half.

So the server fits

```
T(t) = T_ambient - (T_ambient - T_0) · e^(-k·t)
```

over a sliding window of recent readings. In log space that is a straight
line, so `k` comes from a least-squares fit, and the time remaining has a
closed form. Against a simulated cook with realistic sensor noise the estimate
tracks the true answer to within about a minute over an hour.

It refuses to guess rather than guessing wrong. You get no estimate when there
is under ~75 s of history, when the temperature is not rising, or when ambient
is below the target — that last case says *turn up the heat*, because the
centre can never pass the air around it. Estimates are smoothed on the way up
so opening the lid does not make the countdown leap.

Carryover and resting are handled like the MEATER app does it: each preset
knows how far the centre keeps climbing off the heat, so the countdown runs to
a **pull temperature** below the target, and "ready in" includes the rest.

## Presets

67 combinations across beef, pork, lamb, veal, chicken, turkey, duck, fish and
game. Targets follow MEATER's own published guidance where it exists
(medium-rare beef 54–57 °C, pork 63 °C, chicken 74 °C, salmon 52–54 °C) and the
usual doneness chart elsewhere. Override or add presets in config — see the
example file.

## Troubleshooting

**No probes found.** Take the probe out of its charging block. Close the MEATER
phone app: a probe talks to one client at a time, and the app will hold it.

**`Operation already in progress`.** Two connects raced on one adapter. The
server serialises them and retries; it resolves itself.

**Connects then drops.** Check `signal_strength`. These probes are small and
the metal of a grill blocks 2.4 GHz well; the server reconnects with backoff.

**Decoding looks wrong on a newer probe.** `meater-server gatt <address>` dumps
the GATT table and decodes the two characteristics it knows, which is enough to
tell whether the layout changed.

## The protocol

The probe exposes a vendor service, `a75cc7fc-c956-488f-ac2a-2dbc08b63a04`:

- `7edda774-045e-4bbf-909b-45d1991a2876` — six little-endian bytes: tip, an
  ambient reading, and an ambient offset. A raw value is `(raw + 8) / 16` °C.
  Ambient is the tip plus `max(0, (reading − min(48, offset)) · 16 · 589 / 1487)`,
  where the constants are the probe's own calibration.
- `2adb4877-68d8-4884-bd3c-d83853bf27b8` — a little-endian word, 0–10, in tens
  of a percent of charge.

Probes advertise under the Apption Labs company ID `0x037B`.

This comes from the community's reverse engineering, not from a vendor
specification. It is verified against real hardware: a probe at room
temperature reports `38 01 29 00 26 00`, which decodes to 20.0 °C.

## Development

```sh
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest        # 71 tests
.venv/bin/python -m ruff check src tests
```

The ESPHome implementation is tested by driving it with `aioesphomeapi` — the
same client Home Assistant uses — over a real socket: handshake, entity
enumeration, state streaming, and every command type, in both plaintext and
encrypted modes. The framing is additionally checked frame-by-frame against
that library's own frame helpers.

## Licence

MIT.

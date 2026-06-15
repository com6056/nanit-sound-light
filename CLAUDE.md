# Nanit Sound + Light — maintainer & agent notes

A Home Assistant custom integration for the Nanit Sound + Light. It is a
`cloud_push` client: it authenticates to the Nanit cloud over REST, then holds a
protobuf WebSocket to the device for real-time state and control.

> **This is a PUBLIC repository.** Never commit personal names, internal
> hostnames or domains, the private infrastructure repo this integration is
> developed alongside, or unrelated private projects — keep it strictly about
> this integration. A pre-commit leak scan enforces this: `.githooks/leak-scan.sh`
> blocks any commit whose staged content matches a **gitignored** local denylist
> (`.leak-denylist.local`, seeded from `.leak-denylist.local.example` — the real
> terms live only there so they never enter this repo). Enable it per clone with
> `git config core.hooksPath .githooks`; override a false positive with
> `LEAK_SCAN_SKIP=1 git commit`. A `.githooks/pre-push` gate also runs CI's ruff
> checks (`format --check` + `check`) in a container before each push so a lint
> error can't slip in and break CI; bypass with `LINT_SKIP=1 git push`.

## Layout

```
custom_components/nanit_sound_light/
  api.py          # transport: auth, the websocket, protobuf encode/decode, reconnect
  coordinator.py  # DataUpdateCoordinator: device state, command coalescing, pins
  entity.py       # CoordinatorEntity base
  switch.py       # power            light.py   # brightness + color
  select.py       # sound track      number.py  # volume
  sensor.py       # temp, humidity, battery %, wifi RSSI, firmware version
  binary_sensor.py # battery charging
  config_flow.py  # auth + MFA + reauth
  sound_light.proto / sound_light_pb2.py   # wire schema (regenerate via ci.sh)
  brand/          # HA brand images (icon + light/dark wordmark, 1x/2x)
tests/            # offline test suite (see Testing)
```

`brand/` holds the integration's brand images, served in-repo by HA 2026.3+'s
brands proxy (no `home-assistant/brands` PR needed). They're the official Nanit
assets extracted from the apps' vector drawables: the **icon** is the standalone
Sound + Light app's (`com.nanit.lite`) adaptive launcher — the slate-blue lamp
mark (`ic_launcher_foreground`) on its `#040433` background color — and the
**wordmark** logo is the main app's `nanit_logotype`. To regenerate from a newer
APK: decode the binary AXML VectorDrawables (e.g. via `androguard`; resolve the
adaptive-icon background color from `resources.arsc` via its `ARSCParser`),
translate to SVG, render with `cairosvg`. Image spec:
https://github.com/home-assistant/brands.

## How it works (and what NOT to undo)

One physical device is exposed as several HA entities (switch/light/select/number),
all backed by one coordinator + one websocket. The non-obvious design exists to
fix two real, recurring failures — don't revert these without understanding why:

- **Backend readiness gate.** On a fresh remote connect the device's first frame
  is `Message{backend}` reporting whether the physical device is attached behind
  the relay (`Backend.device.status`: `Disconnected=0`/`Connected=1`). The official
  app sends **nothing** until `Connected` — firing `GetSettings`/commands into a
  still-`Disconnected` relay is what produced our command latency. So `connect_device`
  no longer pings on open; `is_device_attached()` latches on the backend `Connected`
  frame (and, defensively, on any genuine `Response`), `wait_for_device_attached()`
  gates sends, and entity `available` requires it. A command still falls back to a
  best-effort send if the frame never arrives (a missed/renamed frame mustn't brick
  control), but the ack-await below then surfaces any real failure.
- **One request in flight + await-ack.** Mirrors the app's `SocketRequestManager`:
  **every** send (control AND the state-ping / saved-sounds polls) goes through one
  `_transact` helper that registers a future keyed by the message id, sends, then
  **awaits the `Response` whose `requestId` matches** (`COMMAND_ACK_TIMEOUT`, 10s),
  under a per-device send lock so transactions never overlap on the wire. Control
  sends `require_ack=True` (a non-2xx status or timeout **raises** → coordinator
  rolls back the optimistic UI); polls send `require_ack=False` (still serialized +
  drained, but a timeout/non-2xx is swallowed). The app awaits every request incl.
  `GetSettings`; a poll that released the lock before its response (the old behavior)
  could overlap a command unacked — the exact "bursts of unacked transactions" that
  wedge the device. Don't reintroduce a send that bypasses `_transact`. (The
  `requestId` correlation is real now — distinct from the pin-guard's id, which stays
  logging-only. All requests use unique ids via `_next_message_id`, starting at 1 like
  the app's `AtomicInteger`; `sessionId` is a random per-connection token.)
- **Command coalescing.** A scene toggles power + sound + volume + light at once.
  Sent as separate protobuf messages they race, and out-of-order responses make
  the device end up in the wrong state (classic symptom: a "turn on" scene leaves
  it off). The coordinator therefore gathers commands arriving within
  `COMMAND_COALESCE_DELAY` and flushes them as **one** combined `Settings` message
  — the same "apply a preset" shape the official app uses. Don't go back to
  one-message-per-field.
- **Pin-guard.** After a command, the affected fields are "pinned" for
  `COMMAND_PIN_SECONDS` so a stale device echo (or a racing confirmation ping)
  can't flap a just-commanded value back. A pin releases early the moment the
  device confirms the value, so genuine later external changes still flow. The
  monotonic message id is **logging only** — neither the device nor this
  integration correlates responses by it, so this time-based pin (not the id) is
  what prevents the flap.
- **Optimistic + rollback.** Commands apply optimistic state immediately for a
  snappy UI; a failed send rolls that back so the UI never shows a state the
  device didn't accept.
- **Power vs light on/off.** The device has a single power primitive, `isOn`,
  owned by the **switch** (`switch.turn_on/off` → bare `Settings{isOn}`). The
  **light** is disabled independently via `color.noColor` so white noise keeps
  playing when you turn the light off — `light.turn_off` sends a bare
  `Settings{color{noColor: true}}` (NOT the old `noColor + brightness:1.0`, whose
  `brightness:1.0` was ambiguous and didn't move the right read-back).
  `build_control_message` omits any color sub-field not provided, so the device's
  stored color survives an off/on cycle. `light.turn_on` keeps the "set
  `sound:'No sound'` when the device was off" guard so flipping the light on
  doesn't unexpectedly resume audio (intentional divergence from the app, which
  sends bare `isOn`).
- **WebSocket keepalive / reconnect.** The device keeps its socket alive with
  **protocol-level ping/pong (~20s)** — there is no app-level keepalive frame, so
  rely on the WS ping (`WS_PING_INTERVAL`), don't invent a heartbeat message. On a
  drop, reconnect **proactively** with backoff `0 → 2 → 5 → 7s` (don't wait for
  the 30s poll). The handler task is kept referenced so it can't be GC'd, and a
  send while disconnected raises rather than silently no-op'ing.

## Protocol facts worth knowing

- Control + state ride on a protobuf `Message { request, response, backend }`;
  control is `Request.settings`, device state arrives as `Response.settings`, and
  `backend` is the readiness frame (`Backend.device.status`). `Message.backend` was
  `bytes` and is now a structured `Backend` message at the **same tag 3** — both
  are wire type 2, so the change is wire-compatible.
- `Settings` fields used here: `brightness=1, color=2, volume=3, sound=4, isOn=5,
soundList=6, temperature=7, humidity=8`. Newer firmware/app builds **append**
  higher-numbered fields; protobuf skips unknown tags, so the schema above stays
  correct and `sound_light.proto` does not need changes for them.
- Secrets: the account password is **not** persisted to `.storage` (only email +
  refresh token); auth responses are redacted before debug logging. Keep it that
  way.
- **Diagnostics ride separate query requests, NOT the GetSettings poll.** Battery
  (`sensor` % + `binary_sensor` charging), wifi RSSI (`sensor`, diagnostic, SSID/
  BSSID/channel as attrs, registry-disabled by default), and firmware version
  (`sensor`, diagnostic) come from three distinct request types the coordinator
  poll issues best-effort: `GetStatus{all:true}` → `Response.status.battery`
  (`StateOfCharge` is a coarse 5-bucket enum → `_SOC_TO_PERCENT`), `Network{getStatus}`
  → `Response.networkStatus.currentAp`, `Firmware{info}` → `Response.firmware`
  (a bare `FirmwareInfo`). The `info`/`getStatus` request fields are present-but-
  empty `Empty` markers (`SetInParent()`). Battery+wifi poll every cycle; firmware
  is fetched once (it's static). All go through `_transact` with a shorter
  `DIAGNOSTICS_ACK_TIMEOUT` so a firmware that ignores them can't hold the send
  lock. **Tags are from the app's `@ProtoNumber` descriptors, NOT element-index+1**
  (that heuristic is off-by-one whenever a message has explicit `@ProtoNumber` or a
  high-tag field like `sessionId=200` — `Request.getStatus=11`, `Response.firmware=6`,
  `networkStatus=8`, `status=9`).

## Testing

Offline, never touches a real device — a `block_nanit_network` fixture fails any
test that resolves `*.nanit.com`.

```
./tests/run.sh            # runs pytest in a throwaway python container
./tests/run.sh -k color   # extra args pass through to pytest
```

- `test_protobuf_contract.py` — schema lock (proto tags, incl. `backend`) + parse
  round-trip + backend-frame → `is_device_attached` + Response→pending-ack resolve.
- `test_control_message.py` — combined-command atomicity + id monotonicity + the
  bare-`noColor` light-off encoding + `sessionId` stamping.
- `test_websocket_reconnect.py` — reconnect backoff, send reaches socket, and
  proactive reconnect after a server drop (against an in-process fake server that
  now also sends a backend `Connected` frame and acks control requests). Covers the
  attach gate, ack-on-success, non-2xx rejection, and ack-timeout.

The heavier **Home Assistant fixture** suite lives in `tests_ha/` (it installs
Home Assistant, so it's a separate run — `./tests_ha/run.sh`). It drives the real
coordinator/entities with a mocked api to cover the logic the offline suite can't:
command coalescing, the pin-guard, optimistic rollback on a failed send, and
entity availability. Keep the two suites in separate processes — both import the
generated protobuf module and would double-register its descriptors otherwise.

CI (`.github/workflows/ci.yml`) runs ruff + prettier + protobuf-drift check +
hassfest + HACS validation + both pytest jobs. Regenerate the protobuf with `ci.sh`.

## Release-polish status

Addressed in the pre-release pass:

- Entities go **unavailable** on socket/cloud outage (base entity gates on
  `last_update_success` + `is_websocket_connected`).
- Setup raises `ConfigEntryNotReady` / `ConfigEntryAuthFailed`; **reauth** is
  self-contained (re-prompts for the password, handles MFA, rotates the token).
- The `cloud_push` poll no longer busy-waits — it's a light backup over the push
  socket.
- `websockets>=13.0` (we use the modern `additional_headers` connect API); setup
  fails loudly on a protobuf import error. `protobuf` is left broad on purpose so
  it's satisfied by whatever HA ships rather than forcing a conflicting upgrade.
- HACS hygiene: no `country` gate, `config.abort` strings, `integration_type`,
  info-spam demoted, emoji removed from logs.
- Coordinator/entity behavior is covered by the `tests_ha/` Home Assistant fixture
  suite (coalescing, pin-guard, rollback, availability, auth/reauth, poll).

A manifest `quality_scale` is intentionally omitted — it's a formal HA
assessment against documented rules, not a self-asserted label, so it's left off
rather than claimed.

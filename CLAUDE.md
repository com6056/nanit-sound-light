# Nanit Sound + Light: maintainer & agent notes

A Home Assistant custom integration for the Nanit Sound + Light. It is a
`cloud_push` client: it authenticates to the Nanit cloud over REST, then holds a
protobuf WebSocket to the device for real-time state and control.

> **This is a PUBLIC repository.** Never commit personal names, internal
> hostnames or domains, the private infrastructure repo this integration is
> developed alongside, or unrelated private projects. Keep it strictly about
> this integration. A pre-commit leak scan enforces this: `.githooks/leak-scan.sh`
> blocks any commit whose staged content matches a **gitignored** local denylist
> (`.leak-denylist.local`, seeded from `.leak-denylist.local.example`. The real
> terms live only there so they never enter this repo). Enable it per clone with
> `git config core.hooksPath .githooks`. Override a false positive with
> `LEAK_SCAN_SKIP=1 git commit`. The same pre-commit hook also runs **ruff** (on
> staged Python) and **prettier** (on staged `json`/`md`/`yml`), both in throwaway
> containers: they auto-fix what they can (ruff `format` + safe `--fix`, prettier
> `--write`), **re-stage** those fixes into the commit, and block only on what
> can't be fixed (a ruff issue or a prettier parse error), so a lint or format
> error can't slip into a push and break CI. Bypass with `LINT_SKIP=1 git commit`.

## Layout

```
custom_components/nanit_sound_light/
  api.py          # transport: auth, the websocket, protobuf encode/decode, reconnect
  coordinator.py  # DataUpdateCoordinator: device state, command coalescing, pins
  entity.py       # CoordinatorEntity base
  switch.py       # power            light.py   # brightness + color
  select.py       # sound track      number.py  # volume
  sensor.py       # temp, humidity, battery %, wifi RSSI, firmware, connection type
  binary_sensor.py # battery charging
  config_flow.py  # auth + MFA + reauth
  sound_light.proto / sound_light_pb2.py   # wire schema (regenerate via ci.sh)
  brand/          # HA brand images (icon + light/dark wordmark, 1x/2x)
tests/            # offline test suite (see Testing)
```

`brand/` holds the integration's brand images, served in-repo by HA 2026.3+'s
brands proxy (no `home-assistant/brands` PR needed). They're the official Nanit
assets extracted from the apps' vector drawables: the **icon** is the standalone
Sound + Light app's (`com.nanit.lite`) adaptive launcher (the slate-blue lamp
mark (`ic_launcher_foreground`) on its `#040433` background color), and the
**wordmark** logo is the main app's `nanit_logotype`. To regenerate from a newer
APK: decode the binary AXML VectorDrawables (e.g. via `androguard`, resolving the
adaptive-icon background color from `resources.arsc` via its `ARSCParser`),
translate to SVG, render with `cairosvg`. Image spec:
https://github.com/home-assistant/brands.

## How it works (and what NOT to undo)

One physical device is exposed as several HA entities (switch/light/select/number),
all backed by one coordinator + one websocket. The non-obvious design exists to
fix two real, recurring failures. Don't revert these without understanding why:

- **Backend readiness gate.** On a fresh remote connect the device's first frame
  is `Message{backend}` reporting whether the physical device is attached behind
  the relay (`Backend.device.status`: `Disconnected=0`/`Connected=1`). The official
  app sends **nothing** until `Connected`. Firing `GetSettings`/commands into a
  still-`Disconnected` relay is what produced our command latency. So `connect_device`
  no longer pings on open, `is_device_attached()` latches on the backend `Connected`
  frame (and, defensively, on any genuine `Response`), `wait_for_device_attached()`
  gates sends, and entity `available` requires it. A command still falls back to a
  best-effort send if the frame never arrives (a missed/renamed frame mustn't brick
  control), but the ack-await below then surfaces any real failure.
- **One request in flight + await-ack, NO re-send.** Mirrors the app's
  `SocketRequestManager`: **every** send (control AND the state-ping / saved-sounds
  polls) goes through one `_transact` helper that registers a future keyed by the
  message id, sends, then **awaits the `Response` whose `requestId` matches**
  (`COMMAND_ACK_TIMEOUT`, 10s), under a per-device send lock so transactions never
  overlap on the wire. **A slow/absent ack on a live socket does NOT re-send and
  does NOT roll back**: the device is busy, not gone, and re-sending piles
  duplicate commands onto an already-overloaded device, which makes it stop
  responding for ~30s and then flush the whole backlog at once (observed on-device
  2026-06-15). The app never retries either. So a control timeout is accepted
  optimistically (the pin holds the UI, the device pushes real state when it
  catches up, the 30s poll reconciles). Only an actual socket **drop** or an
  explicit **non-2xx rejection** raises, so the coordinator rolls back. Polls
  (`require_ack=False`) are still serialized + drained but swallow timeouts. The app
  awaits every request incl. `GetSettings`. A poll that released the lock before its
  response (the old behavior) could overlap a command unacked: the exact "bursts of
  unacked transactions" that wedge the device. Don't reintroduce a send that
  bypasses `_transact`, and **don't re-add command-level retry** (it was removed
  for the reason above). (The `requestId` correlation is real, distinct from the
  pin-guard's id, which stays logging-only. All requests use unique ids via
  `_next_message_id`, starting at 1 like the app's `AtomicInteger`. `sessionId` is a
  random per-connection token.) Validated on-device 2026-06-15: a 20-command
  hammer acked sub-2s with zero duplicates/stalls.
- **mDNS resolver needs the `zeroconf` dependency.** The coordinator imports
  `homeassistant.components.zeroconf` to resolve the device's `.local` address, so
  the manifest declares `after_dependencies: ["zeroconf"]` (hassfest fails
  otherwise). It's `after_` (not a hard dep) because local is best-effort, a
  cloud-only user doesn't need it.
- **Connection Type sensor.** A diagnostic enum sensor (`local`/`cloud`) reflecting
  `api.active_transport()`, what's actually carrying sends right now. Backed by
  `_active_connection_key` (local preferred). Unavailable when the device is
  unreachable (base entity gates on `is_websocket_connected`).
- **Command coalescing.** A scene toggles power + sound + volume + light at once.
  Sent as separate protobuf messages they race, and out-of-order responses make
  the device end up in the wrong state (classic symptom: a "turn on" scene leaves
  it off). The coordinator therefore gathers commands arriving within
  `COMMAND_COALESCE_DELAY` and flushes them as **one** combined `Settings` message,
  the same "apply a preset" shape the official app uses. Don't go back to
  one-message-per-field.
- **Pin-guard.** After a command, the affected fields are "pinned" for
  `COMMAND_PIN_SECONDS` so a stale device echo (or a racing confirmation ping)
  can't flap a just-commanded value back. A pin releases early the moment the
  device confirms the value, so genuine later external changes still flow. The
  monotonic message id is **logging only**. Neither the device nor this
  integration correlates responses by it, so this time-based pin (not the id) is
  what prevents the flap.
- **Optimistic + rollback.** Commands apply optimistic state immediately for a
  snappy UI, and a failed send rolls that back so the UI never shows a state the
  device didn't accept.
- **Power vs light on/off.** The device has a single power primitive, `isOn`,
  owned by the **switch** (`switch.turn_on/off` sends a bare `Settings{isOn}`). The
  **light** turns off by dimming to `brightness:0` so white noise keeps playing,
  leaving whole-device power to the switch. `color.noColor` is white-versus-color,
  NOT light on/off (verified on-device), so `light.turn_off` sends a bare
  `Settings{brightness:0}`, not a `noColor` write. The light's `is_on` is therefore
  "device powered AND brightness > 0". `build_control_message` omits any color
  sub-field not provided, so the device's stored color survives an off/on cycle.
  `light.turn_on` keeps the "set `sound:'No sound'` when the device was off" guard
  so flipping the light on
  doesn't unexpectedly resume audio (intentional divergence from the app, which
  sends bare `isOn`).
- **WebSocket keepalive / reconnect.** The device keeps its socket alive with
  **protocol-level ping/pong (~20s)**. There is no app-level keepalive frame, so
  rely on the WS ping (`WS_PING_INTERVAL`), don't invent a heartbeat message. On a
  drop, reconnect **proactively** with backoff `0 → 2 → 5 → 7s` (remote,
  `0 → 3 → 10 → 60 → 90s` for local, don't wait for the 30s poll). The handler
  task is kept referenced so it can't be GC'd, and a send while disconnected
  raises rather than silently no-op'ing.
- **Auth-rejection backoff + local token invalidation.** A handshake rejected
  with an auth status is handled apart from a transient drop. On a LOCAL 401/403
  the cached per-device token is dropped (`_device_tokens.pop`) so the next
  attempt refetches a fresh one. The device rotates that token server-side and
  can rotate it before our cached copy's clock expiry, so a 401/403 is the only
  signal the cached token went stale. Without this we would re-present the stale
  token and loop on 403 until a reload. Separately, once consecutive auth
  rejections on a transport cross `AUTH_REJECT_BACKOFF_THRESHOLD` (4), that
  transport switches from the fast app-matching backoff to a long, quiet
  `AUTH_REJECT_RETRY_INTERVAL` (120s) and stops logging each failure at ERROR
  (loud for the first few, one WARNING at the threshold, then debug). This keeps
  a wedged device (reachable on the LAN, TLS answers on `:442`, but the app layer
  refuses all auth until a power cycle) from flooding the log with thousands of
  ERROR lines. The long interval is enforced two ways: the reconnect loop sleeps
  it, AND a per-key `_auth_reject_until` timestamp short-circuits `_connect_transport`
  itself (before the `/udtokens` fetch and the handshake). The timestamp gate is
  what throttles the OTHER connect driver, the 30s coordinator poll (it reaches
  `_connect_transport` via `ensure_websocket_connection` -> `connect_device`, which
  the reconnect-loop delay does not cover), so a wedged device hits `/udtokens` at
  most once per interval instead of several times per poll. A clean connect resets
  the per-key count and timestamp, so a normal transient drop keeps the fast
  backoff, and the first few sub-threshold rejections still retry fast and refetch
  the token so a genuine token rotation self-heals quickly.
  Auth statuses are 401/403 for local and 401/403/404 for the relay (a 404 on
  `user_connect` means the relay holds no session for the device, so retrying
  fast is pointless). Status is read defensively from the websockets exception
  (`InvalidStatus.response.status_code` on >= 13, `InvalidStatusCode.status_code`
  on older builds) via `_handshake_status`.
- **Dual transport: prefer local (LAN), fall back to remote (relay).** The cloud
  relay (`wss://remote.nanit.com/speakers/<uid>/user_connect/`) is laggy because
  it sits up while the physical device is idle behind it. On the same LAN the app
  talks to the speaker directly, so we do too: each device can have BOTH a `local`
  and a `remote` socket open at once (keyed `baby_uid::transport`), and sends pick
  the **local** socket when it's up (`_active_connection_key`), falling back to
  remote. Device-level state (attachment, the one-in-flight ack map, the send
  lock, sessionId) is shared across a device's transports, only the URL + auth
  token differ. The local URL is the deterministic mDNS name
  `wss://Nanit-<speaker_uid>.local:442` (NO path, unlike the relay), local auth is
  `Authorization: token <device_token>` (a **per-device** token from
  `GET /speakers/<uid>/udtokens`, NOT the user access token), and local TLS is
  **trust-all** (`_build_insecure_ssl_context`: `check_hostname=False`,
  `CERT_NONE`) because the device presents a self-signed cert the app accepts
  unconditionally. Local is **best-effort and self-healing**: if the device-token
  fetch fails or `Nanit-<uid>.local` doesn't resolve on the HA host, the local
  connect is swallowed and the integration stays on the relay. Availability =
  ANY transport up. **The backend readiness frame is relay-only**, so a local
  socket marks `is_device_attached` the moment it connects (a direct LAN socket
  means the device is present and reachable). A relay socket still waits for the
  Connected frame. That is what lets a pure-local connection (cloud down) bootstrap
  its poll, which otherwise gates on attachment. Enabled by default. The
  `enable_local_connection` entry option (default `True`) can turn it off. All RE
  for this lives in the private infra repo (not here). **Validated end-to-end on a
  real device 2026-06-15** (mDNS resolve, device-token auth, trust-all TLS,
  prefer-local sends, cloud fallback, and a 20-command burst with no stalls). The
  send path still degrades to remote if any local assumption fails, so a bad guess
  can't brick control.
- **Entity naming uses `has_entity_name`.** The base entity sets
  `_attr_has_entity_name = True` and each entity provides a short label (or `None`
  for the device-class default, like Temperature or Battery). Home Assistant
  composes "<device> <label>". Light, Power, Volume, and Sound pass explicit
  labels (this device is multi-function, so the light is "Light" rather than the
  unnamed primary entity), Firmware passes "Firmware", and Connection Type names
  itself through its `translation_key`. The `unique_id` values stay
  `f"{device_uid}_{entity_type}"`, so they are stable across this change.

## Protocol facts worth knowing

- Control + state ride on a protobuf `Message { request, response, backend }`.
  Control is `Request.settings`, device state arrives as `Response.settings`, and
  `backend` is the readiness frame (`Backend.device.status`). `Message.backend` was
  `bytes` and is now a structured `Backend` message at the **same tag 3**, and both
  are wire type 2, so the change is wire-compatible.
- `Settings` fields used here: `brightness=1, color=2, volume=3, sound=4, isOn=5,
soundList=6, temperature=7, humidity=8`. Newer firmware/app builds **append**
  higher-numbered fields, but protobuf skips unknown tags, so the schema above stays
  correct and `sound_light.proto` does not need changes for them.
- Secrets: the account password is **not** persisted to `.storage` (only email +
  refresh token), and auth responses are redacted before debug logging. Keep it that
  way.
- **Diagnostics ride separate query requests, NOT the GetSettings poll.** Battery
  (`sensor` % + `binary_sensor` charging), wifi RSSI (`sensor`, diagnostic, SSID/
  BSSID/channel as attrs, registry-disabled by default), and firmware version
  (`sensor`, diagnostic) come from three distinct request types the coordinator
  poll issues best-effort: `GetStatus{all:true}` → `Response.status.battery`
  (`StateOfCharge` is a coarse 5-bucket enum → `_SOC_TO_PERCENT`), `Network{getStatus}`
  → `Response.networkStatus.currentAp`, `Firmware{info}` → `Response.firmware`
  (a bare `FirmwareInfo`). The `info`/`getStatus` request fields are present-but-
  empty `Empty` markers (`SetInParent()`). Battery and wifi poll every cycle.
  Firmware is fetched once (it's static). They are sent fire-and-forget via
  `_send_no_wait` (not `_transact`), so a firmware that ignores a query can't hold
  the send lock. The responses are still drained and parsed by the handler. The
  SSID, BSSID, and firmware strings are run through `_clean_device_string` (length
  clamp plus printable check) before being exposed, like the sound-track list.
  **Tags are from the app's `@ProtoNumber` descriptors, NOT element-index+1**
  (that heuristic is off-by-one whenever a message has explicit `@ProtoNumber` or a
  high-tag field like `sessionId=200`: `Request.getStatus=11`, `Response.firmware=6`,
  `networkStatus=8`, `status=9`).

## Testing

Offline, never touches a real device. A `block_nanit_network` fixture fails any
test that resolves `*.nanit.com`.

```
./tests/run.sh            # runs pytest in a throwaway python container
./tests/run.sh -k color   # extra args pass through to pytest
```

- `test_protobuf_contract.py`: schema lock (proto tags, incl. `backend`) + parse
  round-trip + backend-frame → `is_device_attached` + Response→pending-ack resolve.
- `test_control_message.py`: combined-command atomicity + id monotonicity + the
  bare-`noColor` light-off encoding + `sessionId` stamping.
- `test_websocket_reconnect.py`: reconnect backoff, send reaches socket, and
  proactive reconnect after a server drop (against an in-process fake server that
  now also sends a backend `Connected` frame and acks control requests). Covers the
  attach gate, ack-on-success, non-2xx rejection, and slow-ack-without-resend.
- `test_local_connection.py`: the direct-LAN transport, with the deterministic mDNS
  URL, trust-all TLS context, device-token fetch/parse (incl. ms→s expiry scaling
  and 404 → no token), and the routing (prefer-local, fall back to remote when
  local drops, availability while one transport is down, local-disabled connects
  remote only). Runs two in-process fakes (local + remote) and reuses
  `_FakeNanit` from `test_websocket_reconnect.py`.

The heavier **Home Assistant fixture** suite lives in `tests_ha/` (it installs
Home Assistant, so it's a separate run via `./tests_ha/run.sh`). It drives the real
coordinator/entities with a mocked api to cover the logic the offline suite can't:
command coalescing, the pin-guard, optimistic rollback on a failed send, and
entity availability. Keep the two suites in separate processes, since both import the
generated protobuf module and would double-register its descriptors otherwise.

CI (`.github/workflows/ci.yml`) runs ruff + prettier + protobuf-drift check +
hassfest + HACS validation + both pytest jobs. `ci.sh` mirrors most of that
locally (ruff, prettier, protobuf regen, and hassfest via the same container image
CI uses) so a CI failure like a missing manifest dependency is caught before
pushing. Run it before a release. The hassfest step mounts only `custom_components`
so the gitignored `.re/` decompile tree (which has stray `manifest.json` files)
can't trip it. The test runners cache pip downloads in a named volume, so only the
first run pays the install cost.

`scripts/live_test.py` is a manual harness for testing api.py changes against a
**real device** without a HA deploy (loads the api standalone, auths, connects
local + cloud, prints status/state). Safe by default (read-only). Set
`NANIT_SEND_TEST=1` for a gentle light demo. Creds via env
(`NANIT_REFRESH_TOKEN` or `NANIT_EMAIL`/`NANIT_PASSWORD`), optional
`NANIT_DEVICE_IP`. Not part of CI, it needs real creds + device.

## Release-polish status

Addressed in the pre-release pass:

- Entities go **unavailable** on socket/cloud outage (base entity gates on
  `last_update_success` + `is_websocket_connected`).
- Setup raises `ConfigEntryNotReady` / `ConfigEntryAuthFailed`, and **reauth** is
  self-contained (re-prompts for the password, handles MFA, rotates the token).
- The `cloud_push` poll no longer busy-waits, it's a light backup over the push
  socket.
- `websockets>=13.0` (we use the modern `additional_headers` connect API), and setup
  fails loudly on a protobuf import error. `protobuf` is left broad on purpose so
  it's satisfied by whatever HA ships rather than forcing a conflicting upgrade.
- HACS hygiene: no `country` gate, `config.abort` strings, `integration_type`,
  info-spam demoted, emoji removed from logs.
- Coordinator/entity behavior is covered by the `tests_ha/` Home Assistant fixture
  suite (coalescing, pin-guard, rollback, availability, auth/reauth, poll).

A manifest `quality_scale` is intentionally omitted. It's a formal HA
assessment against documented rules, not a self-asserted label, so it's left off
rather than claimed.

# Nanit Sound + Light — maintainer & agent notes

A Home Assistant custom integration for the Nanit Sound + Light. It is a
`cloud_push` client: it authenticates to the Nanit cloud over REST, then holds a
protobuf WebSocket to the device for real-time state and control.

## Layout

```
custom_components/nanit_sound_light/
  api.py          # transport: auth, the websocket, protobuf encode/decode, reconnect
  coordinator.py  # DataUpdateCoordinator: device state, command coalescing, pins
  entity.py       # CoordinatorEntity base
  switch.py       # power            light.py   # brightness + color
  select.py       # sound track      number.py  # volume      sensor.py  # temp/humidity
  config_flow.py  # auth + MFA + reauth
  sound_light.proto / sound_light_pb2.py   # wire schema (regenerate via ci.sh)
tests/            # offline test suite (see Testing)
```

## How it works (and what NOT to undo)

One physical device is exposed as several HA entities (switch/light/select/number),
all backed by one coordinator + one websocket. The non-obvious design exists to
fix two real, recurring failures — don't revert these without understanding why:

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
- **WebSocket keepalive / reconnect.** The device keeps its socket alive with
  **protocol-level ping/pong (~20s)** — there is no app-level keepalive frame, so
  rely on the WS ping (`WS_PING_INTERVAL`), don't invent a heartbeat message. On a
  drop, reconnect **proactively** with backoff `0 → 2 → 5 → 7s` (don't wait for
  the 30s poll). The handler task is kept referenced so it can't be GC'd, and a
  send while disconnected raises rather than silently no-op'ing.

## Protocol facts worth knowing

- Control + state ride on a protobuf `Message { request, response }`; control is
  `Request.settings`, device state arrives as `Response.settings`.
- `Settings` fields used here: `brightness=1, color=2, volume=3, sound=4, isOn=5,
  soundList=6, temperature=7, humidity=8`. Newer firmware/app builds **append**
  higher-numbered fields; protobuf skips unknown tags, so the schema above stays
  correct and `sound_light.proto` does not need changes for them.
- Secrets: the account password is **not** persisted to `.storage` (only email +
  refresh token); auth responses are redacted before debug logging. Keep it that
  way.

## Testing

Offline, never touches a real device — a `block_nanit_network` fixture fails any
test that resolves `*.nanit.com`.

```
./tests/run.sh            # runs pytest in a throwaway python container
./tests/run.sh -k color   # extra args pass through to pytest
```

- `test_protobuf_contract.py` — schema lock (proto tags) + parse round-trip.
- `test_control_message.py` — combined-command atomicity + id monotonicity.
- `test_websocket_reconnect.py` — reconnect backoff, send reaches socket, and
  proactive reconnect after a server drop (against an in-process fake server).

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

Still nice-to-have before tagging a release: soften the README's hardcoded
sound-count / "tested" claims, set a real `LICENSE` holder/year, and consider a
manifest `quality_scale`.
```

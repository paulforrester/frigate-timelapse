# HA Dashboard Notes

## Conditional Cards
- Never use `condition: template` in conditional cards — it causes
  "Conditions are Invalid" errors in this HA setup.
- Always use `condition: state` with `state_not` or `state` instead.
- Example of the correct pattern:

```yaml
type: conditional
conditions:
  - condition: state
    entity: sensor.hl_l8360cdw_status
    state_not: ready
card:
  type: custom:mushroom-template-card
  primary: "Printer: {{ states('sensor.hl_l8360cdw_status') }}"
  secondary: >-
    BK {{ states('sensor.hl_l8360cdw_black_toner_remaining') }}% C {{
    states('sensor.hl_l8360cdw_cyan_toner_remaining') }}% M {{
    states('sensor.hl_l8360cdw_magenta_toner_remaining') }}% Y {{
    states('sensor.hl_l8360cdw_yellow_toner_remaining') }}%
  icon: mdi:printer-alert
  icon_color: red
  tap_action:
    action: more-info
    entity: sensor.hl_l8360cdw_status
```

## Camera Entity Migration (2026-05-24)
- All dashboards migrated from direct ONVIF/Axis/VIVOTEK entities to Frigate entities
- Frigate entities do NOT use a `frigate_` prefix — entity IDs are plain camera names
- Card type migrated from `picture-glance` / `picture-entity` to `custom:advanced-camera-card`
- `custom:advanced-camera-card` uses `dimensions.aspect_ratio` (not top-level `aspect_ratio`)
- Entity mapping:
  - `camera.great_room_axis`              → `camera.greatroom`
  - `camera.garage_axis`                  → `camera.garage`
  - `camera.doorbell_profile000_mainstream` → `camera.doorbell`
  - `camera.backyard_pool_8134_profile1`  → `camera.pooldeck`
  - `camera.backyard_spa_8134_profile1`   → `camera.poolspa`
  - `camera.sideyard_west_8134_profile1`  → **unchanged** (no Frigate equivalent yet)
- New cameras added to dashboards: `camera.frontdoor`, `camera.backyardeastreo`, `camera.backyardwestreo`
- Dashboards updated: overview, cameras, security, outdoors
- `backyardwestreo` ≠ sideyard_west — they are different physical cameras at different locations

## Frigate Integration
- Frigate add-on version: 0.17.1-416a9b7 (config schema 0.17-0)
- Frigate HACS integration version: 5.14.1 (installed in custom_components/frigate)
- Frigate internal URL: `http://ccab4aaf-frigate-fa:5000` (resolvable from HA core; IP 172.30.33.3)
- Integration config entry ID: `01KSDHSRCAMWF47VX8WFGK58PX` (connected 2026-05-24)
- Cameras exposed to HA via Frigate (8 total):
  - `garage` — Axis M1054 (Scrypted) + direct RTSP detect stream
  - `greatroom` — via Scrypted
  - `doorbell` — via Scrypted
  - `frontdoor` — via Scrypted
  - `pooldeck` — via Scrypted (VIVOTEK FD8134)
  - `poolspa` — via Scrypted
  - `backyardwestreo` — via Scrypted (Reolink Duo 3 POE west lens)
  - `backyardeastreo` — via Scrypted (Reolink Duo 3 POE east lens)
- Entity naming pattern (no `frigate_` prefix):
  - `camera.<name>` — live stream
  - `binary_sensor.<name>_motion` — motion detection
  - `binary_sensor.<name>_person_occupancy` — person detected
  - `binary_sensor.<name>_<object>_occupancy` — per-object occupancy
  - `sensor.<name>_person_count` — running count
  - `sensor.<name>_camera_fps` / `_detection_fps` — performance
  - `switch.<name>_detect` / `_recordings` / `_snapshots` — controls
  - `image.<name>_<object>` — latest snapshot for that object
- Frigate add-on config: `/addon_configs/ccab4aaf_frigate-fa/config.yaml`
- MQTT: `host: 172.30.33.0`, prefix: `frigate`, user: `mqtt`

## Reolink Integration
- All Reolink cameras are managed by Scrypted — no direct HA integration should exist.
- Four Reolink ignored entries were targeted for deletion on 2026-05-24:
  - `reolink1` (192.168.33.110) — entry `01JFZR2Q38BT0ZPT6TKP8S40FH`
  - `reolink2` (192.168.33.111) — entry `01JFZR2SD47MM2Z369AGCYWVGR`
  - `reolinkhub` (192.168.33.188) — entry `01JM8RZGV8E3YDF7Y3GR3NWN3Y`
  - `camera1` (192.168.33.52) — entry `01JMZZDX8CY7FH93WBGWE5A3ED`
- A backup named `pre-reolink-cleanup-2026-05-24` (backup_id: `be558d06`) was created before cleanup.
- If Reolink devices are auto-discovered by HA in the future, dismiss/ignore them — Scrypted is the canonical owner.
- **API note:** HA 2026.5 does not expose an API to delete `source=ignore` config entries. Deletion must be done via Settings → Devices & Services → "Ignored integrations" → Delete.

## Camera Layout (final)
- One camera per column, placed as the **last card** inside each `vertical-stack` column.
- Use `type: picture-glance` with `aspect_ratio: "16:9"` (colon-separated, quoted string).
- Do NOT use `horizontal-stack` for cameras.
- Column assignments: Left → Great Room (`camera.great_room_axis`), Middle → Garage (`camera.garage_axis`), Right → Doorbell (`camera.doorbell_profile000_mainstream`).
- `picture-glance` requires `entity`, `camera_image`, and `entities:` (use `[]` if no overlay icons needed).
- `aspect_ratio: 16x9` (no quotes, `x` separator) is silently ignored by HA — always use `"16:9"`.

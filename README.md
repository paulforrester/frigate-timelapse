# Home Assistant — Wekiva House Mods

Local working directory for HA configuration files, automations, dashboards, and
deploy scripts. All changes target `ha.flypig.net:8123` (HA OS 2026.5.3).

---

## Quick reference

| Thing | Value |
|-------|-------|
| HA URL | `https://ha.flypig.net:8123` |
| SSH | `ssh -i ~/.ssh/id_ha root@ha.flypig.net` |
| Config root | `/config/` on the HA host |
| Token | `ha_access_token` (this directory) |
| Timezone | `America/Los_Angeles` |

---

## Lovelace Dashboards

Eight custom dashboards built from scratch. Deploy any of them with:

```bash
python3 deploy_dashboard.py dashboard_<name>.yaml
```

| File | Dashboard | URL path | Contents |
|------|-----------|----------|---------|
| `dashboard_overview.yaml` | Home | `/overview` | Whole-home summary — lights, climate, presence, quick scenes |
| `dashboard_rooms.yaml` | Rooms | `/rooms` | Per-room device cards: Great Room, Kitchen, Master Bedroom, Kids rooms, Office, Garage, Laundry |
| `dashboard_media.yaml` | Media | `/media` | Sonos 5.1 surround + all Sonos zones, Apple TVs, HomePods |
| `dashboard_cameras.yaml` | Cameras | `/cameras` | All 8 Frigate cameras (see Camera section below) |
| `dashboard_security.yaml` | Security | `/security` | Cameras, door/window sensors, motion |
| `dashboard_outdoors.yaml` | Outdoors | `/outdoors` | Front entry, backyard, pool/spa, sprinkler zones, hydroponics |
| `dashboard_energy.yaml` | Energy | `/energy` | Tesla Powerwall + solar live flow, history graphs, grid import/export |
| `dashboard_battery.yaml` | Battery Health | `/battery-health` | Per-device battery levels + forecast widgets (see Battery section below) |

### Lovelace card rules

- **Never use `condition: template`** in conditional cards — causes "Conditions are Invalid" in this setup. Always use `condition: state` with `state` or `state_not`.
- Cameras use `custom:advanced-camera-card` with `dimensions.aspect_ratio` (not top-level `aspect_ratio`).
- Entity IDs for Frigate cameras have **no `frigate_` prefix** (e.g. `camera.garage`, `camera.doorbell`).

---

## Frigate Camera Integration

Migrated from direct ONVIF/Axis/VIVOTEK entities to Frigate. All cameras go through Scrypted → Frigate → HA.

| Frigate entity | Physical camera |
|----------------|----------------|
| `camera.garage` | Axis M1054 (Scrypted) |
| `camera.greatroom` | Axis (Scrypted) |
| `camera.doorbell` | ONVIF doorbell (Scrypted) |
| `camera.frontdoor` | (Scrypted) |
| `camera.pooldeck` | VIVOTEK FD8134 (Scrypted) |
| `camera.poolspa` | (Scrypted) |
| `camera.backyardeastreo` | Reolink Duo 3 POE — east lens |
| `camera.backyardwestreo` | Reolink Duo 3 POE — west lens |

**Frigate add-on:** `ccab4aaf_frigate-fa`, internal URL `http://ccab4aaf-frigate-fa:5000`  
**HACS integration:** `custom_components/frigate`

Per-camera entity pattern:
- `binary_sensor.<name>_motion` / `binary_sensor.<name>_person_occupancy`
- `sensor.<name>_person_count`
- `switch.<name>_detect` / `_recordings` / `_snapshots`

---

## Battery Monitoring

### What it does

`battery_forecast.py` queries HA history via WebSocket, fits a linear regression to
each battery sensor's decline curve, and predicts when it will reach 15%. Results are
written to `battery_forecast.json` and pushed to HA as virtual `input_number` sensors
named `input_number.battery_forecast_<slug>`.

The forecast runs weekly (Sunday midnight) and on every HA startup. The Battery Health
dashboard visualises the predictions.

### Automations

| File | Alias | Trigger | Action |
|------|-------|---------|--------|
| `automation_battery_warning.yaml` | Battery Warning — Below 20% | Any battery sensor < 20% for 5 min | Push + persistent notification listing all low devices |
| `automation_battery_critical.yaml` | Battery Critical — Below 10% | Any battery sensor < 10% for 2 min | High-priority push notification |
| `automation_battery_daily.yaml` | Battery Daily Check — 9 AM | Daily 9:00 AM | Scans all battery sensors (numeric + binary `battery_low`); notifies if any < 20% |
| `automation_battery_forecast_14day.yaml` | Battery Forecast — 14-Day Warning | Any forecast sensor < 14 days | Push + persistent notification with device name, level, predicted date |
| `automation_battery_forecast_schedule.yaml` | Battery Forecast — Weekly Update | Sunday 00:00 | Runs `shell_command.run_battery_forecast` on the HA host |
| `automation_battery_forecast_startup.yaml` | Battery Forecast — Restore on HA Startup | HA start (90 s delay) | Re-populates virtual sensors lost across restarts |

### Deploy

```bash
# First-time full deploy (SSH + automations + dashboard):
python3 deploy_battery_forecast.py

# Automations + dashboard only (HA host already configured):
python3 deploy_battery_forecast.py --skip-ssh

# Dashboard only:
python3 deploy_dashboard.py dashboard_battery.yaml
```

`deploy_battery_forecast.py` handles: copying `battery_forecast.py` to `/config/` via
SCP, patching `configuration.yaml` to add the `shell_command`, deploying automations
via REST API, and deploying the dashboard via WebSocket.

### configuration.yaml changes

```yaml
shell_command:
  run_battery_forecast: >-
    python3 /config/battery_forecast.py
    --ha-url https://localhost:8123
    --token-file /config/battery_forecast_token
    --output /config/battery_forecast.json

recorder:
  purge_keep_days: 90   # needed for meaningful trend data
```

---

## SSL Certificate Auto-Renewal

### What it does

Monitors the Let's Encrypt wildcard cert (`*.flypig.net`) and automatically renews
it before it expires. Sends push notifications to both phones, then restarts the
Let's Encrypt add-on which writes the new cert and restarts HA on its own.

**Cert details:**
- File: `/ssl/star_flypig_net_cert.pem` (+ `star_flypig_net_key.pem`)
- Type: wildcard `*.flypig.net` via Let's Encrypt DNS-01 challenge
- Managed by: `core_letsencrypt` add-on (run-and-stop style)
- Current expiry: **25 June 2026** (23 days as of 2 June 2026)

### Files

| File | Purpose |
|------|---------|
| `ha_command_line_sensors.yaml` | `sensor.ssl_cert_expiry_days` — polls cert daily, returns days-to-expiry |
| `ha_shell_commands.yaml` | `shell_command.renew_ssl_cert` — runs `ha apps restart core_letsencrypt` |
| `automation_ssl_renewal.yaml` | Fires at 8:00 AM when cert ≤ 7 days out; sends notifications then triggers renewal |
| `deploy_ssl_renewal.py` | Deploys all of the above via SSH + REST API |

### Automation flow

1. **Notify first** — persistent notification + push to both phones (Papa's Toy + phfiphone) fires *before* the shell command, so it survives the HA restart the add-on triggers
2. **Renew** — `ha apps restart core_letsencrypt` runs certbot; if renewal occurs, the add-on restarts HA automatically
3. No explicit `homeassistant.restart` step — the add-on handles it

### Sensor

`sensor.ssl_cert_expiry_days` — integer, unit `days`. Uses Python `cryptography`
package (not `openssl`, which is absent from the HA container) to parse the PEM file.
Polls hourly. Returns `-1` if the cert file is missing or unreadable.

### configuration.yaml changes

```yaml
shell_command:
  renew_ssl_cert: "ha apps restart core_letsencrypt"

command_line: !include ha_command_line_sensors.yaml
```

### Deploy

```bash
python3 deploy_ssl_renewal.py

# If HA host config is already patched (e.g. re-deploying after an automation change):
python3 deploy_ssl_renewal.py --skip-ssh
```

---

## Integration Fixes

### Plex auth token

Plex session tokens expire. When the "Authentication expired" repair appears:

1. Get a new permanent token via OAuth PIN flow:
   ```
   POST https://plex.tv/api/v2/pins?strong=true
   → user authorizes at https://app.plex.tv/auth#?clientID=...&code=...
   → GET https://plex.tv/api/v2/pins/{id}  →  authToken
   ```
2. Patch the token in HA storage (on the HA host):
   ```bash
   jq '(.data.entries[] | select(.domain=="plex") | .data.server_config.token) = "NEW_TOKEN"' \
     /config/.storage/core.config_entries > /tmp/new.json \
     && cp /tmp/new.json /config/.storage/core.config_entries
   ```
3. `ha core restart`

Plex server: `http://192.168.33.9:32400` ("Rangiroa"). The token lives at
`data.server_config.token` inside the Plex entry in `core.config_entries` — not at
the top-level `data.token`.

### Zigbee2MQTT OTA pinning

Seven Hue devices were generating constant OTA update notifications. Fix applied:

1. Per-device in `/config/zigbee2mqtt/configuration.yaml`:
   ```yaml
   disabled_ota_upgrades: true
   ota:
     disable_automatic_update_check: true
   ```
2. Global OTA section in same file:
   ```yaml
   ota:
     disable_automatic_update_check: true
     zigbee_ota_override_index_location: /config/zigbee2mqtt/ota_override.json
   ```
3. Removed `update` key from those 7 devices in `state.json` **while Z2M was fully stopped**:
   ```bash
   ha apps stop 45df7312_zigbee2mqtt
   # edit state.json
   ha apps start 45df7312_zigbee2mqtt
   ```

Z2M add-on slug: `45df7312_zigbee2mqtt`. Note: per-device `ota.disable_automatic_update_check` is silently ignored in Z2M 2.x — the global setting is required.

---

## Deploy scripts

| Script | Does |
|--------|------|
| `deploy_dashboard.py` | Deploy a single dashboard YAML via WebSocket |
| `deploy_battery.py` | Deploy battery automations + dashboard (no SSH steps) |
| `deploy_battery_forecast.py` | Full battery forecast deploy: SSH file copy + config patch + automations + dashboard |
| `deploy_ssl_renewal.py` | Deploy SSL renewal: SSH config patch + sensor file + automation |

All scripts read the token from `ha_access_token`. SSH steps use `~/.ssh/id_ha`.

---

## Environment notes

See `CLAUDE.md` for full details. Key points:

- **HA OS host shell has no `python3`, `openssl`, or `docker`** — all local text manipulation must be done here and piped back via SSH stdin (`cat > /config/file`)
- **`ha addons` is deprecated** — use `ha apps`
- **New `command_line:` sensors need a full HA restart** to appear; `reload_all` is not enough
- **`persistent_notification.*` are not entity states in HA 2025+** — check the sidebar or query via WebSocket `persistent_notification/get`
- The `cryptography` Python package is available in the HA core container and is the right tool for parsing PEM certificates

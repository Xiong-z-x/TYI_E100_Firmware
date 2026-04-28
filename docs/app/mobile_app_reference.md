# Mobile App Reference

## Goal

This file is the reference for the future mobile app that connects to:

- `control-gateway`
- `media-gateway` through WebRTC negotiated by `control-gateway`

The app is intended for local network debugging, viewing video, checking telemetry, and reading logs.

## User Roles

- `viewer`
  - can discover device
  - can view video
  - can view telemetry
  - can view logs allowed by policy
- `maintainer`
  - all viewer permissions
  - can pair device
  - can restart the currently advertised WHEP video session
  - can export logs
  - can rotate pairing sessions
  - can list and revoke paired clients
- `operator`
  - can trigger VLM grounding and planner-assisted navigation goals

## Recommended Screens

### 1. Discovery

List fields:

- device name
- model
- online state
- pairing state
- firmware version
- signal or network hint

Primary actions:

- pair
- connect
- refresh

Client rules:

- treat the shared pairing code as a credential and never log or display it after entry
- handle `PAIRING_FAILED` by prompting the user to retry code entry instead of reusing a stale challenge
- surface `pairedClientCount` / `pairedClientLimit` from `/healthz` in developer diagnostics when pairing capacity is close to full
- expect old refresh tokens to become invalid after rotation, logout, stale-client pruning, or maintainer revocation

### 2. Device Home

Cards:

- flight connection
- video connection
- battery
- GPS
- mode
- latest alarms
- container health

Primary actions:

- open live video
- open telemetry details
- open logs

### 3. Live Video

Widgets:

- low-latency WebRTC video surface
- server-advertised profile badge, for example 480p15
- round-trip and packet loss indicators
- request keyframe action
- snapshot action

Client rules:

- read `availableProfiles` / `selectedProfile` from device state before creating a session
- use the advertised profile in `POST /v1/webrtc/sessions`
- Do not hardcode `1080p30`; current low-load RealSense path may advertise `480p15`
- trust the `profile`, `viewerUrl`, `whepUrl`, and `rtspUrl` returned by the session response

### 4. Navigation

Widgets:

- natural-language target input, for example `飞向塑料袋橘子`
- latest VLM grounding result preview for the submitted snapshot
- live red-dot reprojection from the server-resolved 3D target anchor
- computed target distance and relative pose
- planner status and goal publish result

Primary action:

- call `POST /v1/navigation/ground-and-plan`

Recommended response handling:

- show the grounded target box for the submitted snapshot and the resolved 3D target anchor together
- during flight, draw the red dot from server reprojection or server target-anchor state, not from stale submitted-frame pixels
- surface `NAVIGATION_TARGET_NOT_FOUND`, `NAVIGATION_DEPTH_FAILED`, `ODOM_STALE`, and `PLANNER_UNAVAILABLE` directly in UI

Optional overlays:

- battery
- mode
- altitude
- speed
- GPS fix

### 5. Telemetry

Sections:

- pose and local position
- battery and power
- GPS and satellites
- FCU state
- estimator health

### 6. Logs and Alarms

Views:

- live log tail
- filter by source
- filter by level
- alarms only
- export diagnostic package

Sources:

- `flight-core`
- `control-gateway`
- `media-gateway`

### 7. Paired Clients

Views:

- paired client list
- client role and platform
- last seen and last refresh timestamps
- revoke client action

## Connection Model

### Discovery

Use:

- mDNS first
- UDP broadcast fallback

### Business Session

Use:

- HTTPS for request-response APIs
- WSS for state stream, alarms, and log tail

### Video

Use:

- WebRTC only

The app should not directly control the media service without going through `control-gateway`.

## Session States

The app should clearly show:

- discovered
- pairing required
- pairing in progress
- connected
- degraded
- video unavailable
- disconnected

## Alarm Levels

- `info`
- `warning`
- `error`
- `critical`

The app home page should surface `warning` and above immediately.

## Example State Payloads

### Device Summary

```json
{
  "deviceId": "e100-0001",
  "displayName": "TYI E100 0001",
  "model": "TYI_E100",
  "firmwareVersion": "0.1.2",
  "gatewayVersion": "0.1.0",
  "paired": true,
  "supportsWebRtc": true,
  "videoProfiles": [
    "480p15"
  ]
}
```

### Flight State

```json
{
  "armed": false,
  "connected": true,
  "mode": "OFFBOARD",
  "batteryPercent": 82,
  "voltage": 15.4,
  "altitudeMeters": 1.7,
  "speedMetersPerSecond": 0.3,
  "gpsFix": 3,
  "satellites": 18
}
```

### Alarm Event

```json
{
  "id": "evt-20260414-001",
  "level": "warning",
  "source": "flight-core",
  "code": "GPS_FIX_DROP",
  "message": "GPS quality degraded",
  "timestamp": "2026-04-14T13:42:11Z"
}
```

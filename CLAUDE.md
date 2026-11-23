# Bird feeder viewer

Local web viewer for a VEVOR FB-A01 smart bird feeder ("Q8" camera module, VicoHome cloud platform). Polls the unofficial VicoHome cloud API for motion events, archives snapshots/clips locally, serves a gallery UI, and offers experimental live view over WebRTC through VicoHome's cloud.

## Run

- `python3 server.py` — poller + web UI at http://localhost:8080
- `python3 server.py --probe` — log in once, print devices and the newest event (secrets hidden), exit.
- Config lives in `.env` (see `.env.example`). Restart the server after editing it.

## Constraints

- **Standard library only.** No pip dependencies; must run on the system Python 3.8/3.9 on macOS and on a Raspberry Pi. No `X | None` annotations, no `match`, no 3.10+ APIs. (The WebSocket client/server for live view is hand-written for this reason.)
- **ffmpeg** is optional (Homebrew at `/opt/homebrew/bin/ffmpeg`); without it clips are not saved.
- **Never log, print, or commit credentials, the API token, or live-view ticket tokens.** `.env` and `data/` are gitignored; `.env` is chmod 600. The live relay logs only message `method` names.
- **No local stream.** The camera (on the home LAN, Wi-Fi module by Sichuan AI-Link) has zero open TCP ports, and its USB-C port is power-only. Don't propose RTSP/ONVIF/local-stream approaches. Live view goes through VicoHome's cloud (see below).
- **One session per VicoHome account**: each login invalidates the previous token (e.g. the phone app). `VicoClient.call` re-logs in once on a non-zero `result` (live-view wake-up calls pass `relogin=False`). Keep polling modest (default 120 s, minimum 30 s) and don't add parallel logins. Recommend a second account with the feeder shared to it.
- The placeholder password `changeme` is treated as unset so we don't hammer the login endpoint with bad credentials.
- The app's live-view component pins its TLS certificates, so mitmproxy can't see the signalling WebSocket; ordinary API calls (`api-us.vicoo.tech`) are not pinned and can be captured with mitmproxy (`postman/live_watch.py` was used for that).

## Layout

- `server.py` — everything backend:
  - `VicoClient` — API client (urllib). Base URL `https://api-{region}.vicoo.tech` (override `VICO_API_BASE`). Adds `countryNo`, `language`, `requestId` and an `app` metadata block to every body, like the app.
  - `Archive` — `data/events.json` keyed by `traceId`; media in `data/media/<traceId>.jpg|.mp4`. Our own bookkeeping fields on events are prefixed `_` (`_local`, `_first_seen`) and survive merges.
  - `poller()` thread — finds the serial via `listuserdevices/v4` (or `VICO_SERIAL`), backfills `BACKFILL_HOURS` on first run, then re-fetches a rolling window each poll; `/api/refresh` wakes it early.
  - Follows deletions made in the VicoHome app: every `PRUNE_EVERY` (600 s) the poll fetches the last `PRUNE_HOURS` (24 h) instead of 1 h, and `Archive.prune_deleted()` drops visits in the fetched window that the cloud no longer lists (files too). Only when the fetch is complete (`events_checked` got the server's `total`), never for older visits (they may just have aged out of the cloud), and never for visits with a recorded clip or `_synthetic` recordings.
  - Home Assistant (optional) — with `HA_TOKEN` (a long-lived access token) set, `push_home_assistant()` runs after every poll and POSTs `/api/states/<entity>` to `HA_URL`: `sensor.bird_feeder_battery`, `binary_sensor.bird_feeder_online`, `sensor.bird_feeder_wifi_signal`, `sensor.bird_feeder_last_visit` (timestamp), `sensor.bird_feeder_visits_today`. No HA YAML needed; such entities disappear on an HA restart until the next poll. Each new failure is logged once; the token is never logged.
  - Live relay — `Upstream` (minimal TLS WebSocket client), `ws_send`/`ws_recv` framing, `Handler.live_ws()` at `GET /api/live/ws`. One live session at a time (`live_lock`); cross-origin WebSocket requests are refused.
  - `Handler` — serves `static/`, `/api/events`, `/api/status` (`?watch=1` = a page is auto-recording), `POST /api/refresh`, `POST /api/recording`, `DELETE /api/video?trace=` (deletes a clip: a `_synthetic` entry goes entirely, a camera visit keeps its snapshot and gets `_local.video_deleted` so the clip isn't re-downloaded), `/api/live/ws`, and `/media/*` (with HTTP Range support; Safari needs it for MP4).
  - Recordings — the account has no cloud plan (`hasVip=false`), so the cloud keeps only a snapshot per visit (`videoUrl` null, 1 s events). Clips come from live view instead: the page records with `MediaRecorder` and POSTs the raw file to `/api/recording?trace=&kind=video|image&ext=mp4|webm|jpg&ts=`. `finish_recording()` remuxes Safari MP4 (audio to AAC) or transcodes Chrome WebM to H.264 with ffmpeg, saving `data/media/<traceId>-rec.mp4` and setting `_local.video` + `_local.recorded`. SD-card clips arrive the same way with `&source=sd` and are saved as `<traceId>-sd.mp4`; `_local.video_source` is `live` or `sd`, and a live upload for a visit that already has its SD clip is refused with 409. MP4 uploads are remuxed (`stream_codecs()` picks copy vs AAC; HEVC is tagged `hvc1`, which Safari needs). Manual recordings use trace `rec-<ms>` and get a `_synthetic` event of their own (with a still). Same-origin check as the live relay. While a page sends `watch=1`, the poller uses 30 s (`WATCH_POLL_SECONDS`) instead of `POLL_SECONDS`.
- `static/index.html` — single-file UI (vanilla JS, hls.js from jsDelivr for un-archived remote clips, browser-native WebRTC for live). Light/dark via CSS tokens on `:root`. Mobile first: live player on the home screen auto-starts on open (muted so autoplay is allowed), pauses when the page is hidden, stops after 5 min (`LIVE_MAX_MS`), and retries once if it fails before the first frame. iOS 26.1+ draws a dark fade/blur over the top ~40pt of home-screen apps, worst with `black-translucent`: the status-bar style is `default` (coloured by `theme-color`), the header stays opaque with no backdrop blur, and `static/manifest.webmanifest` (`display: standalone`) is linked: the owner's other home-screen apps have a manifest and no fade, while this one had only `apple-mobile-web-app-capable`. Changing the status-bar meta or manifest needs the icon removed and re-added. A phone turned sideways (`orientation: landscape` and `max-height: 500px`) shows only the video, live player or open visit; tapping it shows the player controls for 3 s. Pull-to-refresh is hand-rolled (home-screen apps have none; native overscroll is off): it POSTs `/api/refresh` and reloads, or just does the ↻ refresh while a recording or SD download is running. The list shows only visits with a clip (one tile each) unless "Show snapshots" is on (localStorage `feeder.showSnaps`); snapshots are still archived as thumbnails and the Today card still counts all visits. With snapshots shown, visits are grouped by day (older days collapsed) and into bursts within `GROUP_GAP_MS` (10 min); a Today card shows visits per hour. **Auto-record** (header toggle, saved in localStorage): when `/api/status` reports a new `newest` visit under 3 min old, the page opens live view, records `RECORD_SECONDS` (default 20), uploads, and hangs up (unless the viewer interacted). Needs the page open; the server can't do WebRTC. A ⏺ Record button in the player records manually (max 2 min). **With an SD card** (`device.sdCard.total > 0`), Auto-record instead queues the visit for its SD clip (`queueSd`; one camera wake per batch of up to 5, started about `SD_SETTLE_S` (30 s) after the visit so the recording is finished), and opening a visit in the viewer fetches its SD clip automatically (once per page load; a visit you flip away from before its download starts is dropped from the queue). SD sessions never touch the home-screen player: progress shows as a pill over the viewer's picture (`showSdStatus`), the finished clip replaces the snapshot in the open viewer, and live view paused for the download resumes when the viewer closes (`resumeLiveSoon`).
- `postman/` — Postman collection for the API, plus mitmproxy add-ons used for capturing the app (`live_watch.py`, `force_webrtc.py`).

## VicoHome API (undocumented)

Confirmed by capturing the VicoHome iOS app 4.7.0 with this feeder (FB-A01, firmware 1.19.900), Nov 2026. All calls are `POST` JSON; success is `result == 0`; payload is under `data`. Authenticated calls send `Authorization: Bearer <token>` (login returns `data.token.tokenType` = `Bearer`). `-1025 TOKEN_MISSING` = no/invalid token. The older host `api-us.vicohome.io` with a raw token also worked.

| Endpoint | Body (beyond the common fields) | Returns |
|---|---|---|
| `/account/login` | `{email, password, loginType: 0}` | `data.token.{token, tokenType}`, user `id`, `node` |
| `/device/listuserdevices/v4` | `{list: [], supportBindFlow: 0}` | `data.deviceList[]` (same fields as below) |
| `/device/selectsingledevice` | `{serialNumber}` | `deviceName`, `online` (1), `awake`, `batteryLevel` (%), `isCharging`, `chargingMode`, `signalStrength` (dBm), `signalLevel`, `wifiChannel`, `firmwareId`, `newestFirmwareId`, `deviceModel.streamProtocol` (`webrtc`), `deviceSupport.*` |
| `/library/newselectlibrary` | `{startTimestamp, endTimestamp, from, to, marked: -1, missing: -1, tags: [], doorbellTags: []}` (seconds, ints) | `data.list[]`, `data.total`. Paging via `from`/`to` (app sent `from:0,to:1` and got 1 item of `total` 64) |
| `/device/wakeupDevice` | `{serialNumber, voiceReminder: false}` | `{}` |
| `/device/getWebrtcTicket` | `{serialNumber, supportUnlimitedWebsocket: true, …}` | `signalServer` (wss), `websocketPath` (`/iot-channel/connect`), `accessToken`, `id`, `groupId`, `traceId`, `iceServer[]` (TURN on smartvideogo.com:5349), `signalPingInterval`, `appStopLiveTimeout` |
| `/device/getAccessEndpoint` | `{xlinkProtocolCapability: 1}` | "xlink-tcp" relay on port 52000 (proprietary binary; not used here) |

Event fields: `traceId`, `timestamp`, `mediaType` (`image`/`video`), `imageUrl`, `videoUrl`, `serialNumber`, `deviceName`, `birdName`, `birdStory` (null without VicoHome's bird-AI plan), `tags`, `summaryDescription`. `deviceAiEventList` lists what the camera *can* detect, not what it detected.

- Media URLs are signed (Oracle Cloud object storage) and expire, so the archiver downloads them on first sight. A clip's m3u8 may not be ready immediately, and since Nov 2026 the cloud can list a visit before its upload finishes (leaving 2 s clips). `fetch_media()` saves a cloud clip only when `hls_state()` finds the playlist complete (`#EXT-X-ENDLIST`) and unchanged since the previous poll (`_local.playlist_size`), or the visit is older than `HLS_TRUST_AFTER_S` (5 min); it gives up after `HLS_GIVE_UP_S` (45 min). Waiting doesn't use up the `MAX_VIDEO_ATTEMPTS` ffmpeg tries. (This account gets no `videoUrl` today; the logic follows ha-vicohome-bridge.)

## Live view protocol

Signalling is JSON text frames over `wss://<signalServer><websocketPath>`; the server must send WebSocket pings every `signalPingInterval` s (browsers can't, hence the relay). Flow (relay does step 1, the browser does the rest):

1. → `{method: AUTH, clientType: app, status: normal, accessToken, id}`; ← `AUTH_RESPONSE {code: 0}`
2. → `{method: JOIN_LIVE, role: viewer, name: id, group: groupId, traceId, recipientClientId: serial}`; ← `JOIN_LIVE_RESPONSE`, then `PEER_IN` when the camera joins
3. → `{method: TRANSMIT, messageType: SDP_OFFER, messagePayload: base64(JSON{type, sdp}), mode: vicoo, recipientClientId: serial, senderClientId: id, sessionId: "Android-<id>-<ms>", viewerType: a4x_sdk, resolution: 1280x720, version: 0.0.1}`
4. ← `TRANSMIT SDP_ANSWER`; both sides trickle `TRANSMIT ICE_CANDIDATE` with base64(JSON{sdpMid, sdpMLineIndex, candidate})
   **Keep the offer small.** The camera silently ignores a full browser offer (no answer, then it reconnects with `PEER_IN reason=12` and leaves with `PEER_OUT reason=1`). The UI limits it with `setCodecPreferences` to H.265 then H.264 (no RTX/RED/FEC) and PCMU/PCMA/Opus, and skips `.local` and TCP candidates. Send one offer per session; the camera may announce `PEER_IN` more than once, and if it does so before answering, the same offer is sent again (at most every 3 s). `JOIN_LIVE_RESPONSE code 0` means already awake: offer at once. `PEER_OUT` is not fatal (it is also sent when another viewer, e.g. the signed-out phone app, leaves); the page gives up only if the link isn't up 8 s later. `NEED_REAUTH` ends the session.
5. The offer includes a data channel labelled with the serial; on open send `{action: startLive, requestID, connectionID: "", timeStamp, size: medium, resolution: 1280x720}`. Video track arrives.

## SD card clips

The camera records every visit to its microSD card from the motion trigger (full resolution, ~20 s), free. The page fetches those over the live session's data channel (`SdTunnel`, `runSd` in `index.html`), following jbuck2005/ha-vicohome-bridge (`sdfetch.py`, MIT), so the server stays stdlib-only:

- An SD session is a normal live session started with `startLive(0, null, visits)`. It also sends a silent audio track (Web Audio `ConstantSource`) because the bridge found the card listing fails without real audio. After video flows (or 20 s), it sends a framed `stopLive` command, then `httpToDevice POST /dc/rtcconnection/open {deviceSdpMode: 0, serialNumber}`.
- Framing (both ways): `AB CD <type> 00 <stream id:2> <flags:2> <length:4>` + body. We send type `0x18`. Replies per stream: `0x12` with flags 0 = status JSON (`contentLength`), `0x12`/`0x16` otherwise = body chunk, `0x1a` = end. Commands: `{requestID, connectionID: "7893feb", timeStamp, action, parameters}`. HTTP: `{action: "httpToDevice", data: {acceptType: 0, body, headers: [..., "safe-rtc-root-url:https://127.0.0.1:15000", "safe-rtc-version:1"], localDomain: "https://api-local.safemo.com", method, path}}`.
- List: `POST /library/newselectlibrary` over the tunnel with `{fromSDCard: true, storeType: "sd_card", serialNumber: [serial], startTimestamp, endTimestamp, from: 0, to: 2000, ...}`; items whose `traceId` ends in `.mp4` are recordings (`timestamp`, `period`, local `downloadUrl`/`videoUrl`). A visit takes the recording that covers it, else the nearest within 30 s; one recording goes to one visit.
- Download: `GET <downloadUrl path>` (fallback `/dc/video/mp4/<name>?sn=<serial>`) with `Range: bytes=N-`; a download that stalls 6 s is resumed from where it stopped (3 times). The MP4 is uploaded to `/api/recording?...&source=sd`.
- Costs: wakes the camera and takes the account's single live session (signs the phone app out). Not yet tested on this feeder (bridge tested on a CG625; this feeder reports `modelNo` CG625-BD-ST1JSB).

## Open questions

- SD fetch is implemented but untested on this feeder. Unknowns: whether the plain 0.0.1 offer is enough (the bridge uses the compact 0.0.2 SDP from vicolocal), the clips' codec, and whether the silent audio track works in Safari without a user gesture (auto-record runs without one). Without a card, Auto-record still records live view, starting 15-45 s after the visit.
- Next steps borrowed from the other projects: FCM push for ~1 s event latency (ha-vicohome; needs a pip package), and server-side WebRTC (aiortc or go2rtc) so SD fetch and recording run without a browser.

- Live view works end to end in Safari (Nov 2026). Still unknown: which codec the camera picks when offered both (device lists `liveStreamCodecOptions: ["h265"]`), whether `appStopLiveTimeout` needs a periodic keep-alive on the data channel, and why a session can make a loud ringing noise (possibly the `sendrecv` audio transceiver feeding the camera's speaker).
- Whether `from`/`to` in `newselectlibrary` are offsets or a page range; `VicoClient.events` dedupes by `traceId` and stops on an empty page or `total`, so either works.

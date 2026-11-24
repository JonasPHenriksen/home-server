# tapo-automation

Presence based control and lightweight recording for Tapo cameras.

> Sanitized copy: addresses are examples and camera names are numbers. See the
> top-level README.

* When no phone has been seen on the network for a while the cameras are
  **armed** (privacy mode off, motion/person detection and alarm on) and the
  streams are **recorded**.
* As soon as a phone is back the cameras go into **privacy mode** and
  recording stops.
* **Night mode** (off by default, `night_mode.sh on|tonight [HH:MM-HH:MM]`)
  keeps the cameras in the `night` group watching quietly during a time
  window, whatever the presence state: detection on, alarm off.

Frigate used to do the recording. It was far too heavy for "a folder of video
per camera", so it was replaced by `cam_recorder.py`: one `ffmpeg -c copy`
process per camera, supervised by a small Python daemon. No Docker, no
database, no detection, almost no CPU. See [Compared to Frigate](#compared-to-frigate).

## Components

| File | Role |
| --- | --- |
| `check_phones_loop.sh` | Runs `check_phones_and_toggle.sh` every 10 s (systemd `tapo_presence.service`). |
| `check_phones_and_toggle.sh` | The state machine: asks `phone_probe.py` who is home, decides enable/disable, retries incomplete switches with backoff, re-syncs when the night window opens or closes. Writes `.tapo_motion_state`. |
| `phone_probe.py` | Presence detection: every phone, every method, in parallel. |
| `tapo_toggle.py` | Applies enable/disable to the cameras via pytapo. Parallel, with a hard deadline and per-camera sync state. |
| `night_mode.sh` | `on`, `off`, `tonight`, `status` for the night rules, with an optional time window. |
| `night_common.sh` | Shared window parsing for the two shell scripts. |
| `cam_recorder.py` | Records the RTSP streams while `.tapo_motion_state` says `enable` (systemd `cam_recorder.service`). |
| `status_page.py` + `status_page.html` | The web page: state of the house and the recorders, a per-day timeline of clips, thumbnails, playback and download (systemd `status_page.service`, port 8090, HTTPS when configured). |
| `config.json` | Phones, cameras (IP, group, RTSP URL), presence and recording settings. **Gitignored, holds passwords.** See `config.example.json`. |
| `.tapo_credentials` | Tapo camera account: username on line 1, password on line 2. Gitignored. |
| `systemd/` | Unit files. |
| `tests/` | pytest suite (see below). |

### How the pieces fit

```
phone_probe.py ──▶ check_phones_and_toggle.sh ──writes──▶ .tapo_motion_state ──read──▶ cam_recorder.py
                          │                                                            │
   night_mode.sh ──flag──▶└──▶ tapo_toggle.py ──pytapo──▶ cameras ◀──rtsp (ffmpeg)─────┘
```

The state file is written *before* any camera is touched, so the recorder and
the toggle react to the same decision independently. Neither can block the
other.

## Presence detection

`phone_probe.py` runs three methods against every phone at the same time and
returns as soon as one of them answers (typically 0.1 to 0.6 s when someone is
home, `timeout_seconds` + 0.5 s when nobody is):

| Method | What it does | Why |
| --- | --- | --- |
| `icmp` | `ping -c 1` | Cheap and fast, but a sleeping iPhone ignores it completely. |
| `tcp` | connect to port 62078 | iPhones keep this port open even asleep; any other phone answers "connection refused". Both prove it is there. Only silence means absent. |
| `arp` | `ip neigh show` | Runs after the other two: their packets just made the kernel ARP for every phone, so a phone on wifi shows as `REACHABLE`, ICMP or not. |

This matters because the away threshold learns from false triggers and only
ever goes up: with ping alone, a sleeping iPhone looked "missing" so often that
the threshold kept climbing and the cameras armed later and later. With three
methods the misses stop, so the threshold can be reset to start low again:

```bash
rm /home/dietpi/tapo-automation/.tapo_away_threshold
```

```bash
# see what each method says about each phone right now
tapo-env/bin/python phone_probe.py --verbose
```

If the probe itself fails (exit code 2, or killed by its 15 s guard) the loop
assumes someone is home and logs it once. A broken detector must never arm the
cameras.

## Night mode

Normally the `night` camera group behaves like every other camera. Night mode
is for when you want those cameras watching at night while people are home,
without the alarm going off:

```bash
./night_mode.sh on 20:00-04:00      # every night, with this window, until switched off
```

```bash
./night_mode.sh tonight             # default window 21:00-02:00, off by itself when it closes
```

```bash
./night_mode.sh off
```

```bash
./night_mode.sh status              # setting, window, and whether it is active right now
```

The window argument is optional and defaults to 21:00-02:00. It may cross
midnight. Run `on` again with a new window whenever the season changes; the
loop picks the change up within 10 s. Times are host time; `night_mode.sh` prints the
zone next to every time so a UTC host cannot be misread.

**Priority.** While the window is open, a `night` camera is set to privacy off,
motion detection on, alarm off, whether the house is empty or not. The
presence state only decides what happens to it outside the window: armed with
alarm when away, privacy mode when home. The `always` cameras are never
affected by night mode.

Nothing happens from `night_mode.sh` itself: it writes `.tapo_night_mode`
(`start=`, `end=`, optional `expires=`), and the presence loop re-syncs the
cameras when the flag or the window state changes. `tapo_toggle.py` reads the
same file, so both agree on the window. The former `night_cams.sh` did this
with a separate state machine and put the night camera into privacy mode at
the end of its window even when nobody was home; the merged logic recomputes every camera from
the current state instead.

## Why one dead camera used to stall everything

`tapo_toggle.py` configured cameras one after another, with up to five retries
and 2/4/8/16 s sleeps per camera plus pytapo's own 10 s request timeouts.
One unreachable camera cost a minute or more per run, during which the presence
loop was blocked. Frigate was started and stopped through Docker inside the same
run. The retry state was per run, not per camera, so every retry reconfigured the
cameras that were already fine.

Now:

* Cameras are configured **in parallel** on daemon threads.
* The run has a **hard deadline** (`--deadline`, default 60 s). A camera that
  is still busy at the deadline is reported as out of sync and left for the
  next run. A global socket timeout guards against calls without one.
* The shell script wraps the toggle and the probe in `timeout` as a last line
  of defence, and holds a lock so runs never overlap.
* **Per-camera sync state** (`.tapo_cam_state.json`) means a retry only touches
  the cameras that are still wrong. A camera is forgotten from the state before
  it is touched, so a crash half way is retried too.
* Recording has moved into its own daemon with its own per-camera supervision.

## Recording

`cam_recorder.py` starts, for every camera in `config.json` that has an `rtsp`
URL:

```
ffmpeg -rtsp_transport tcp -timeout 5000000 -use_wallclock_as_timestamps 1 ... -i rtsp://... \
       -c:v copy -c:a aac -b:a 32k \
       -f segment -segment_time 600 -segment_atclocktime 1 -strftime 1 \
       -segment_format_options movflags=+frag_keyframe+empty_moov+default_base_moof:flush_packets=1 \
       recordings/<camera>/%Y-%m-%d_%H-%M-%S.mp4
```

* Segments are 10 minutes, cut on the wall clock (`:00`, `:10`, ...), one flat
  folder per camera. Filenames sort chronologically. Open them with any player.
* Video is copied, never re-encoded. Audio is transcoded to AAC because Tapo
  sends PCM which MP4 cannot hold (`"audio": "none"` drops it).
* `stream2` is the camera's substream (640x360 or 1280x720, depending on the
  model), about 30-40 KB/s per camera, or 120 MB per camera per hour of
  recording. `stream1` is the full resolution stream (2304x1296 / 1920x1080) at
  roughly four to six times the size; switch the URL per camera if detail
  matters more than disk. CPU use is the same either way, since the video is
  copied, not re-encoded. There is no useful middle ground short of
  re-encoding, which costs CPU and quality; the Tapo app's quality setting for
  the main stream is the only other knob.
* Each camera has its own backoff (5 s doubling to 5 min, reset after a healthy
  minute). A camera that is offline is simply retried; the others keep going.
* An ffmpeg that stays alive but stops writing (a stuck RTSP session) is
  restarted after `stall_seconds`.
* On stop (`disable`, or service shutdown) ffmpeg is asked to quit with its
  `q` key on stdin plus SIGTERM, and finalises the open segment so it stays
  playable. SIGKILL follows after `stop_grace_seconds`. (ffmpeg 7.0 only notices
  either request through its stdin loop, which is why `-nostdin` is not used.)
* Segments are fragmented MP4 (a self-contained fragment per keyframe) and
  packets are flushed as they arrive, so a segment grows continuously and a
  crash or power cut of the laptop leaves it playable up to the last fragment.
  Only the few seconds still in the kernel cache are lost. Set
  `"fragmented_mp4": false` for classic MP4 if a very old player needs it.
* A camera that loses power or network makes ffmpeg exit within about 7 s;
  the open segment is finalised and playable, and the camera is retried with
  backoff until it is back.
* Wallclock timestamps are kept on purpose: the Tapo RTP timestamps jitter and
  without them ffmpeg reports dozens of non-monotonic DTS warnings per minute.
* Retention: segments older than `retain_days` are deleted every 10 minutes.
  If free space drops below `min_free_gb` the oldest segments go first.
* `recordings/status.json` shows what every camera is doing. Per-camera ffmpeg
  output goes to `logs/ffmpeg_<camera>.log`, the daemon log to
  `logs/cam_recorder.log` (rotated).
* Pushes to the phone (see [Notifications](#notifications)): once when a
  camera has failed `notify_after_failures` times in a row, once more when it
  has been recording again for a healthy minute; once when the disk stays
  below `min_free_gb` after pruning, once more when it is back.

```bash
# show the resolved config and the exact ffmpeg commands (passwords redacted)
tapo-env/bin/python cam_recorder.py --check
```

```bash
# record regardless of the presence state (handy for a manual test, Ctrl-C to stop)
tapo-env/bin/python cam_recorder.py --always
```

### Compared to Frigate

Measured on the server with the cameras recording (privacy-mode streams,
so the video bitrate was tiny; real footage adds well under 1% CPU per camera
because the video is only copied):

| | Frigate (as configured) | cam_recorder.py |
| --- | --- | --- |
| Processes | Docker container: Frigate, go2rtc restreamer, per-camera ffmpeg, SQLite, web UI | one Python process (18 MB) + one ffmpeg per camera (28 MB each) |
| Memory | container plus a 512 MB shared-memory segment and a 1 GB tmpfs cache reserved | about 100 MB in total |
| CPU | constant background load even with detection off | about 2% of one core per camera, 0.5% for the supervisor |
| Storage | 10 s cache segments merged into hourly files in a database-indexed tree | 10-minute fragmented MP4 files, one folder per camera |
| Retention | 14 days continuous and motion | 14 days, plus a free-space guard |
| Viewing | web UI with timeline and events | any file browser or media player, `status.json` for health |
| Recording control | container started and stopped by the toggle script | follows `.tapo_motion_state` on its own, per-camera backoff and stall recovery |
| Not provided any more | | web UI, live view, motion/object events, snapshots, MQTT and Home Assistant integration |

Everything Frigate offered beyond "record these streams" was unused here
(detection was disabled), so nothing you relied on is gone.

### ffmpeg

The recorder uses `ffmpeg` from `PATH` (the Debian package is installed) and
falls back to `bin/ffmpeg` if a static build is placed there. Camera
recordings, logs and `bin/` are excluded from the host's `dietpi-backup`; that
filter and the weekly maintenance job live in [`host-maintenance`](../host-maintenance/).

## Installation

```bash
sudo cp systemd/cam_recorder.service systemd/status_page.service /etc/systemd/system/
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now cam_recorder.service status_page.service
```

```bash
systemctl status cam_recorder.service tapo_presence.service
```

`tapo_presence.service` picks up changes to `check_phones_and_toggle.sh` on its
own, since the loop re-executes it every 10 s. Changes to `cam_recorder.py`,
`status_page.py`, `config.json` or `check_phones_loop.sh` need a service
restart.

On every start of `tapo_presence.service` (so after every boot) the loop
forgets what the cameras are believed to be set to and re-applies the last
requested state to all of them, in case they were power-cycled too.

## Configuration

`config.json` (copy from `config.example.json`, `chmod 600`):

```json
{
  "phones": {
    "phone_1": {"ip": "192.168.50.13"},
    "phone_2": {"ip": "192.168.50.15"}
  },
  "presence": {
    "methods": ["icmp", "tcp", "arp"],
    "tcp_ports": [62078],
    "timeout_seconds": 1.5
  },
  "cameras": {
    "cam_1": {"ip": "192.168.50.16", "group": "always", "rtsp": "rtsp://USER:PASS@192.168.50.16:554/stream2"},
    "cam_2": {"ip": "192.168.50.14", "group": "night",  "rtsp": "rtsp://USER:PASS@192.168.50.14:554/stream2"}
  },
  "recording": {
    "root": "recordings",
    "control_file": ".tapo_motion_state",
    "record_when": "enable",
    "retain_days": 14,
    "min_free_gb": 20,
    "segment_seconds": 600,
    "stall_seconds": 60,
    "audio": "aac",
    "fragmented_mp4": true,
    "notify_command": "/usr/local/bin/ntfy-notify",
    "notify_after_failures": 5
  }
}
```

* `phones`: name them as you like; the names show up in the log.
* `presence.methods`: drop a method to disable it. `timeout_seconds` is the
  total wait when nobody answers.
* `group`: `always` cameras follow presence only. `night` cameras also follow
  night mode, which takes priority over presence while its window is open.
* A camera without `rtsp`, or with `"record": false`, is toggled but not recorded.
* `input_args` / `output_args` (per camera or under `recording`) replace the
  default ffmpeg arguments when a camera needs something special.
* `notify_command`: the push helper (`null` switches the recorder's pushes
  off). `notify_after_failures`: consecutive failures of one camera before a
  push; with the default backoff 5 means about two and a half minutes of a
  camera not recording while it should.
* Other tunables and their defaults are listed at the top of `cam_recorder.py`
  and `phone_probe.py`. Thresholds and cooldowns are at the top of
  `check_phones_and_toggle.sh`.

## Status page

`status_page.py` serves, on port 8090 to the home network and the tailnet,
what Frigate's recordings view used to give, without Frigate: the state of the
house and of every recorder at the top, a day picker, and one 24-hour timeline
row per camera with a block for every recorded segment. Click a block to play
it in the browser, step through the clips of that camera with the thumbnail
strip or the arrow keys, and download the clip on screen with one button. The
recent lines of `tapo.log` and `cam_recorder.log` are at the bottom. The state
refreshes every ten seconds, the timeline every thirty.

It is a single Python file on the standard library plus one HTML file, no
framework and no database: it reads the same state files the other scripts
write, lists the segment files, and serves them with HTTP range support (Safari
refuses to play video without it). Thumbnails are made by ffmpeg on first
request, one at a time, cached under `recordings/.thumbs/` and dropped when
the recorder deletes their clip. A segment's length is taken from its last
write, so a clip cut short by the house filling up again shows its real size.

Recording happens only while nobody is home, so a day with people in the house
has an empty timeline. That is the system working.

```json
"status_page": {
  "port": 8090,
  "bind": "0.0.0.0",
  "auth": "USER:PASSWORD",
  "thumbnails": true,
  "ffmpeg": null,
  "tls_cert": "/home/dietpi/.config/home-tls/laptop.crt",
  "tls_key": "/home/dietpi/.config/home-tls/laptop.key"
}
```

`ffmpeg` is the binary used for thumbnails; `null` means the one the recorder
uses.

`tls_cert` and `tls_key` put the page on HTTPS, `https://192.168.50.10:8090`,
with the server's certificate for the home sites (host-maintenance's README,
"HTTPS with a single-use CA"); both `null`, the default, is plain HTTP.
To add them to an existing `config.json` without opening it, and restart the
page (the file keeps its permissions, and nothing changes if `jq` fails):

```bash
cd ~/tapo-automation && (umask 077 && jq '.status_page += {"tls_cert": "/home/dietpi/.config/home-tls/laptop.crt", "tls_key": "/home/dietpi/.config/home-tls/laptop.key"}' config.json > config.json.new) && chmod --reference=config.json config.json.new && mv config.json.new config.json && ./tapo-env/bin/python3 status_page.py --check && sudo systemctl restart status_page
```

`auth` is HTTP basic authentication. Over HTTPS the password is encrypted
like everything else; over plain HTTP it travels in the clear, so configure
TLS. Leave it empty for no login. The
firewall on the host already limits who can reach the port at all. The login
summary shows the URL while the service is running.

```bash
# resolved configuration
tapo-env/bin/python status_page.py --check
```

## Notifications

The system runs unattended, so its problems are pushed to the phone through
the ntfy server on the same machine (see host-maintenance's README,
"Notifications"; they only reach the phone on the home network, by design). Everything goes through `/usr/local/bin/ntfy-notify`. Each
problem is pushed once, and once more when it is over; normal operation
(arming, disarming, a single failed attempt) is never pushed.

| Push | Priority | Source |
| --- | --- | --- |
| `<camera>: recording keeps failing` after `notify_after_failures` failures in a row, then `<camera>: recording again` after a healthy minute of data | high, low | `cam_recorder.py` |
| `Recording disk is low` when pruning cannot get back above `min_free_gb`, then `Recording disk OK again` | high, low | `cam_recorder.py` |
| `Cameras not switching to enable/disable` after 4 incomplete switches in a row (about 7 minutes of retries), then `Cameras in sync again` | high, low | `check_phones_and_toggle.sh` |
| `Presence probe broken` when `phone_probe.py` itself fails, so the cameras cannot arm, then `Presence probe working again` | high, low | `check_phones_and_toggle.sh` |

The recorder starts the helper in the background and reaps it on the next
tick; the shell script backgrounds it too (with the lock closed, so the next
10 s run is not held up). A slow or dead ntfy can therefore never delay a
camera, and a missing or failing helper is only logged. The shell script
pushes whenever `/usr/local/bin/ntfy-notify` is installed; `TAPO_NOTIFY_CMD`
overrides that (the tests point it at a fake). `.tapo_switch_notified` marks
an incomplete switch that has been pushed, `.tapo_probe_error` a broken probe.

```bash
# try the helper as the user the services run as
ntfy-notify -t video_camera "Camera test" "hello from tapo-automation"
```

## Tests

```bash
tapo-env/bin/python -m pytest
```

The suite needs no cameras or phones and takes about 20 s:

* `test_phone_probe.py`: parallelism, first-answer-wins, timeout bounds, each
  method against local sockets and fake `ping`/`ip`, config validation, CLI.
* `test_check_phones.py`: the bash state machine, run for real against a fake
  probe and a fake toggle: arming threshold, false trigger learning, backoff
  and cap, pending retries superseded by state changes, probe failures,
  timeout guards, lock, night window transitions, night mode expiry, pushes for
  a long incomplete switch and a broken probe (against a fake helper).
* `test_night_mode.py`: `night_mode.sh` on/off/tonight/status, window
  parsing and expiry.
* `test_tapo_toggle.py`: parallelism, deadline, retries, auth lockout handling,
  sync state and crash safety, night mode flag, config errors, exit codes.
* `test_cam_recorder.py`: the supervisor against `tests/fake_ffmpeg.py`:
  control file handling, one camera failing while the other records, backoff
  growth and reset, stall detection, `q`/SIGTERM/SIGKILL escalation, retention
  and disk guard, status file, config validation, credential redaction,
  pushes for a failure streak, its recovery and a low disk (fake helper).
* `test_ffmpeg_integration.py`: the real ffmpeg with a synthetic source through
  the real segmenting arguments, verified with ffprobe. Skipped if no ffmpeg.
* `test_status_page.py`: the web page against a real server on a random port:
  state and timeline APIs, duration estimates, range requests and the download
  header, path traversal and unknown cameras refused, basic auth, thumbnails
  made from a real synthetic clip and pruned with it, config validation.

`pytest` is installed in `tapo-env` (`requirements-dev.txt`).

## Operations

```bash
tail -f tapo.log logs/cam_recorder.log
```

```bash
cat recordings/status.json
```

```bash
# force every camera into the home state, even if believed in sync
tapo-env/bin/python tapo_toggle.py disable all --force
```

State files (all gitignored): `.tapo_motion_state` (last requested state),
`.tapo_cam_state.json` (what each camera is set to), `.tapo_pending`,
`.tapo_retry_after`, `.tapo_switch_fail_streak`, `.tapo_switch_notified`
(incomplete switch backoff and its push),
`.tapo_ping_fail_count`, `.tapo_away_threshold`, `.tapo_enable_time`
(presence detection), `.tapo_night_mode`, `.tapo_night_window` (night mode),
`.tapo_probe_error` (probe currently failing).

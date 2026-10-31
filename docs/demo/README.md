# Recorded dashboard replay

A static page that shows the real dashboard working, with no install: open it, and it plays a recorded run of
EdgeFDL on the simulator. `tools/demo/build_site.py` assembles the site and
[`.github/workflows/pages.yml`](../../.github/workflows/pages.yml) publishes it on GitHub Pages.

| File | Role |
|---|---|
| [`frames.json`](frames.json) | The recording: the responses of `/api/topology`, `/api/status`, `/api/alarms?n=12` and `/api/series?link=<each link>`, round by round, plus the scene marks, the captions and the test conditions (`meta`) |
| [`index.html`](index.html) | Banner, player (play, pause, speed, scene picker, timeline), captions, and an iframe for the dashboard |
| [`replay.js`](replay.js) | Loads the dashboard page into the iframe and answers its `fetch` calls from `frames.json` |

The dashboard itself is not stored here. `build_site.py` copies
[`edgefdl/core/web/index.html`](../../edgefdl/core/web/index.html) into the site unchanged as `dashboard.html`; only at
load time does the player swap the fault-injection panel for the scene picker and remove the CSV export buttons, which
need a server. The site uses relative paths only, so it works from any static file server under any sub-path.

## How a recording is made

`tools/demo/record.py` starts the real `python -m edgefdl serve` (simulation backend, `results/model.pt`) with the
default measurement period, seed and noise, drives it through `POST /api/inject` and `POST /api/clear`, and after each
measurement round stores the responses the dashboard polls. Nothing is written by hand: `frames.json` holds what the
running system returned, and the recorder checks that the file expands back to those responses exactly. Repeated
sub-objects are shared in a pool, and each target's series points are stored once.

The captions are generated from the recording. So is the counter of targets over threshold: it applies the
detector's per-target test (RTT, jitter, loss, unreachable; `FeatureEncoder.anomaly_matrix`) to
`/api/export/measure_log.csv`, against the baseline in the model bundle, for the 36 targets probed from the EMS. The
same recording's per-scene outcomes and test conditions are in
[`results/demo_replay.json`](../../results/demo_replay.json).

## Rebuild and check

```bash
python tools/demo/record.py                    # one run in real time, about 14 minutes; rewrites frames.json
python tools/demo/build_site.py                # assembles build/site
python -m http.server -d build 8000            # then open http://127.0.0.1:8000/site/
python tools/demo/check_page.py --shots /tmp/shots   # needs playwright: serves the site under /EdgeFDL/, compares
                                                     # what the dashboard shows with frames.json, frame by frame
python -m unittest tests.test_demo
```

The story, in order: normal network; 20 ms of extra one-way delay on L1; L6 down, with traffic moving to the backup
path; the OSPF cost of L4 multiplied by 10; 20 ms of delay on L5 and 15% loss on L7 at the same time, repaired one at a
time; device `acc3` down. The fault values are those of the online scenarios in the self-test.

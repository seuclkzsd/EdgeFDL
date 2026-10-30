#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
# The continuous-integration pipeline as one script. .github/workflows/ci.yml and release.yml run its
# steps on GitHub's ubuntu-latest runners; the same script runs unchanged on a developer machine
# (Linux, Python >= 3.8 with requirements.txt installed), so a red CI step can be reproduced locally
# with exactly the same command.
#
#   tools/dev/ci.sh [STEP...]          default: quick
#     licenses   tools/dev/spdx.py: every source file carries its SPDX license header
#     tests      the unit tests: python -m unittest discover -s tests -v; a test skipped because
#                results/model.pt is missing fails the step
#     serve      the online system end to end on the simulation backend: `edgefdl serve` with
#                results/model.pt on a free local port (0.2 s rounds), a link-down fault injected on
#                L4 through POST /api/inject, the link-down alarm on L4 awaited (<= 60 s), the fault
#                cleared through POST /api/clear and the alarm's recovery awaited (<= 60 s), then
#                /api/alarms, the CSV export and the Prometheus /metrics endpoint checked
#     pipeline   a short offline run of `edgefdl pipeline` (baseline, 3,000 training samples plus the
#                validation and test sets, 600 training iterations, evaluation, a 360-round self-test)
#                into a temporary directory; eval_test.json and bench/bench.json must exist and be
#                well-formed. This is a sanity check of the whole chain, not an accuracy check: the
#                published numbers come from the full run (docs/results.md)
#     dist       sdist and wheel into dist/ (python -m build; needs the `build` package); the wheel
#                must carry the dashboard (edgefdl/core/web/index.html), the license files and the
#                `edgefdl` command; installed into a fresh virtual environment, the wheel must pass
#                the serve check above from outside the source tree
#     realstack  the real protocol stack: scripts/lab.sh build && scripts/lab.sh smoke (26 checks of
#                the netns backend with FRR OSPF and tc in a privileged Docker container); needs Docker
#     version    the release checks: the tag (RELEASE_TAG, default v<edgefdl.__version__>) must be
#                v<edgefdl.__version__>, and CHANGELOG.md must hold a dated section
#                "## [<version>] - YYYY-MM-DD" without placeholders; writes the release notes to
#                build/release/notes.md (and version= / tag= to $GITHUB_OUTPUT inside GitHub Actions)
#     release    version + dist, then the release assets in build/release/assets: the wheel and
#                sdist, results/model.pt as edgefdl-demo-model.pt, a tarball of results/ and SHA256SUMS
#     quick      licenses tests serve
#     all        licenses tests serve pipeline dist realstack
#
# Environment: PYTHON (interpreter, default python3); CI_TMP (work directory of serve / pipeline /
# dist, default a new directory under ${TMPDIR:-/tmp} that is removed on exit unless CI_KEEP=1);
# CI_WORKERS / CI_THREADS (pipeline sample-generation processes / training threads, default 2 / 2);
# EDGEFDL_APT_MIRROR (realstack: Debian mirror for the image build, default deb.debian.org);
# EDGEFDL_LAB_IMAGE (realstack: image tag, default edgefdl-lab:latest).
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$root"
export PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
py="${PYTHON:-python3}"
all=(licenses tests serve pipeline dist realstack)
known=(licenses tests serve pipeline dist realstack version release quick all)

steps=()
for s in "${@:-quick}"; do
  case "$s" in
    quick) steps+=(licenses tests serve) ;;
    all) steps+=("${all[@]}") ;;
    *) steps+=("$s") ;;
  esac
done
for s in "${steps[@]}"; do
  case " ${known[*]} " in *" $s "*) ;; *) echo "ci.sh: unknown step '$s' (${known[*]})" >&2; exit 2 ;; esac
done

if [ -n "${CI_TMP:-}" ]; then
  tmp="$CI_TMP"
  mkdir -p "$tmp"
else
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/edgefdl-ci.XXXXXX")"
  [ "${CI_KEEP:-0}" = 1 ] || trap 'rm -rf "$tmp"' EXIT
fi

step() {
  local name="$1" t0=$SECONDS
  shift
  if [ -n "${GITHUB_ACTIONS:-}" ]; then echo "::group::ci: $name"; else echo "== ci: $name"; fi
  "$@"
  [ -z "${GITHUB_ACTIONS:-}" ] || echo "::endgroup::"
  echo "ci: $name passed in $((SECONDS - t0)) s"
}

# ---------------------------------------------------------------------------------------- tests
# tests_check: the unit tests; a test skipped for want of results/model.pt fails the step, so the
# engine tests that need the published model cannot silently stop running
tests_check() {
  local log="$tmp/unittest.log"
  "$py" -m unittest discover -s tests -v 2>&1 | tee "$log"
  [ "${PIPESTATUS[0]}" = 0 ] || return 1
  if grep -q "skipped '无模型'" "$log"; then
    echo "tests: tests skipped because results/model.pt was not found" >&2
    return 1
  fi
}

# ---------------------------------------------------------------------------------------- serve
# serve_check DIR CWD CMD...: run `CMD serve` (CMD: `python -m edgefdl` or an installed `edgefdl`)
# from the directory CWD, files in DIR, and drive it through the REST API.
serve_check() {
  local dir="$1" cwd="$2"
  shift 2
  rm -rf "$dir" && mkdir -p "$dir"
  "$py" - "$dir" "$cwd" "$root/results/model.pt" "$@" <<'PY'
import json, os, signal, socket, subprocess, sys, time, urllib.request

out, cwd, model, edgefdl = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
with socket.socket() as s:                       # a free local port
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
base = f"http://127.0.0.1:{port}"
http = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # local server: never via a proxy
log_path = os.path.join(out, "serve.log")
cmd = edgefdl + ["serve", "--model", model, "--backend", "sim", "--interval", "0.2",
                 "--host", "127.0.0.1", "--port", str(port), "--log-dir", os.path.join(out, "logs")]
print("serve:", " ".join(cmd), "(in %s)" % cwd)
log = open(log_path, "w")
proc = subprocess.Popen(cmd, cwd=cwd, stdout=log, stderr=subprocess.STDOUT)


def get(path):
    with http.open(base + path, timeout=10) as r:
        return r.read()


def post(path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with http.open(req, timeout=10) as r:
        return json.loads(r.read())


def wait(what, cond, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if proc.poll() is not None:
            raise SystemExit(f"serve: the server exited with code {proc.returncode} while waiting for {what}")
        try:
            v = cond()
        except OSError:
            v = None
        if v:
            return v, time.time() - t0
        time.sleep(0.25)
    raise SystemExit(f"serve: no {what} within {timeout} s")


def status():
    return json.loads(get("/api/status"))


def l4_alarm(s):
    return next((a for a in s.get("active_alarms") or []
                 if a.get("link_id") == "L4" and a.get("fault_type") == "link_down"), None)


ok = False
try:
    s, dt = wait("/api/status", lambda: (lambda x: x if x.get("round", 0) >= 3 else None)(status()), 90)
    print(f"serve: up after {dt:.1f} s: backend {s['backend']}, model {s['model']}, round {s['round']}, "
          f"{len(s['link_health'])} links")
    assert s["backend"] == "sim" and not s.get("active_alarms"), "a fresh server must have no active alarm"
    topo = json.loads(get("/api/topology"))
    assert len(topo["links"]) == 15 and "L4" in {l["id"] for l in topo["links"]}, "unexpected topology"
    assert b"<html" in get("/").lower(), "the dashboard page is missing"

    r = post("/api/inject", {"type": "link_down", "link": "L4"})
    assert r.get("ok"), r
    r0 = status()["round"]
    a, dt = wait("link-down alarm on L4", lambda: l4_alarm(status()), 60)
    s = status()
    print(f"serve: alarm {a['alarm_id']} ({a['fault_type']} on {a['link_id']}, severity {a['severity']}) "
          f"{dt:.1f} s / {s['round'] - r0} rounds after the injection")
    others = [x for x in s.get("active_alarms") or [] if x is not a and x.get("alarm_id") != a["alarm_id"]]
    if others:
        print("serve: note: further active alarms:", [(x["fault_type"], x["link_id"]) for x in others])

    assert post("/api/clear", {}).get("ok")
    _, dt = wait("recovery of the L4 alarm", lambda: l4_alarm(status()) is None, 60)
    print(f"serve: the alarm cleared {dt:.1f} s after the fault was cleared")

    hist = json.loads(get("/api/alarms?n=20"))
    assert any(x.get("alarm_id") == a["alarm_id"] for x in hist), "alarm missing from /api/alarms"
    csv = get("/api/export/alarm_log.csv").decode("utf-8")
    assert "L4" in csv and "link_down" in csv, "alarm missing from alarm_log.csv"
    metrics = get("/metrics").decode("utf-8")
    values = {ln.split()[0]: float(ln.split()[-1]) for ln in metrics.splitlines() if ln and not ln.startswith("#")}
    for name in ("edgefdl_round", "edgefdl_alarms_total", "edgefdl_alarms_active", 'edgefdl_link_health{link="L4"}'):
        assert name in values, f"/metrics lacks {name}"
    assert values["edgefdl_alarms_total"] >= 1 and values["edgefdl_round"] >= 1, metrics
    print(f"serve: /metrics: {len(values)} samples, edgefdl_round {values['edgefdl_round']:.0f}, "
          f"edgefdl_alarms_total {values['edgefdl_alarms_total']:.0f}")
    ok = True
finally:
    if proc.poll() is None:
        proc.send_signal(signal.SIGINT)          # the server stops its engine on KeyboardInterrupt
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    log.close()
    if not ok:
        print("serve: server log (tail):")
        print("".join(open(log_path, encoding="utf-8", errors="replace").readlines()[-40:]))
PY
}

# ------------------------------------------------------------------------------------- pipeline
pipeline_check() {
  local dir="$tmp/pipeline"
  rm -rf "$dir"
  "$py" -m edgefdl pipeline --out "$dir" --n 3000 --iters 600 --workers "${CI_WORKERS:-2}" \
    --threads "${CI_THREADS:-2}" --bench-rounds 360 2>&1 | tee "$tmp/pipeline.log"
  "$py" - "$dir" <<'PY'
import json, math, os, sys

d = sys.argv[1]
ev = json.load(open(os.path.join(d, "eval_test.json"), encoding="utf-8"))
types = ("link_down", "delay", "loss", "jitter", "bandwidth", "route_drift", "normal")
assert set(types) <= set(ev["per_type"]), f"eval_test.json: per_type has {sorted(ev['per_type'])}"
for t in types:
    row = ev["per_type"][t]
    assert row["n"] > 0, (t, row)
    for k, v in row.items():
        if k.endswith(("_acc", "_rate")):
            assert isinstance(v, (int, float)) and math.isfinite(v) and 0.0 <= v <= 1.0, (t, k, v)
print("pipeline: eval_test.json (%d samples): joint accuracy by type %s" % (
    sum(ev["per_type"][t]["n"] for t in types),
    ", ".join(f"{t} {ev['per_type'][t]['joint_acc']:.2f}" for t in types if "joint_acc" in ev["per_type"][t])))

b = json.load(open(os.path.join(d, "bench", "bench.json"), encoding="utf-8"))
assert {"latency", "noise", "scenarios", "summary"} <= set(b), sorted(b)
lat, nz = b["latency"], b["noise"]
assert lat["params"] > 0 and 0 < lat["torch"]["forward_p99_ms"] < 1e4, lat
assert nz["rounds"] == 360 and nz["engine_errors"] == 0, nz
rows = b["scenarios"]["rows"]
assert rows and all({"link", "type", "alarmed"} <= set(r) for r in rows), "bench.json: scenario rows"
print(f"pipeline: bench.json: {lat['params']} parameters, forward p99 {lat['torch']['forward_p99_ms']} ms, "
      f"{nz['rounds']} noise rounds with {nz['engine_errors']} engine errors, {len(rows)} scenario runs")
PY
}

# ----------------------------------------------------------------------------------------- dist
dist_check() {
  local dir="$tmp/dist-venv"
  mkdir -p dist
  rm -f dist/edgefdl-*.whl dist/edgefdl-*.tar.gz
  "$py" -m build --outdir dist .
  "$py" - "$root" <<'PY'
import glob, sys, tarfile, zipfile
from pathlib import Path

root = Path(sys.argv[1])
version = {}
exec((root / "edgefdl" / "__init__.py").read_text(encoding="utf-8"), version)
v = version["__version__"]
whl = glob.glob(str(root / "dist" / f"edgefdl-{v}-*.whl"))
sdist = glob.glob(str(root / "dist" / f"edgefdl-{v}.tar.gz"))
assert len(whl) == 1 and len(sdist) == 1, (whl, sdist)
names = zipfile.ZipFile(whl[0]).namelist()
need = ["edgefdl/__init__.py", "edgefdl/core/web/index.html", "edgefdl/cli.py"]
missing = [n for n in need if n not in names]
assert not missing, f"wheel lacks {missing}"
info = f"edgefdl-{v}.dist-info/"
ep = zipfile.ZipFile(whl[0]).read(info + "entry_points.txt").decode()
assert "edgefdl = edgefdl.cli:main" in ep, ep
for lic in ("LICENSE", "NOTICE"):
    if (root / lic).is_file():
        assert any(n.startswith(info) and n.endswith("/" + lic) for n in names), f"wheel lacks {lic}"
snames = tarfile.open(sdist[0]).getnames()
assert f"edgefdl-{v}/edgefdl/core/web/index.html" in snames, "sdist lacks the dashboard"
print(f"dist: {Path(whl[0]).name} ({len(names)} files), {Path(sdist[0]).name} ({len(snames)} files)")
PY
  # The wheel on its own: a fresh environment (dependencies from the running interpreter), the
  # installed `edgefdl` command run from outside the source tree, the serve check repeated with it.
  rm -rf "$dir"
  "$py" -m venv --system-site-packages "$dir"
  "$dir/bin/python" -m pip install --quiet --no-deps --no-index dist/edgefdl-*.whl
  (cd "$dir" && bin/edgefdl --version && bin/python -c \
    'import edgefdl, sys; sys.exit(0 if edgefdl.__file__.startswith(sys.prefix) else "edgefdl not from the wheel")')
  serve_check "$tmp/dist-serve" "$dir" "$dir/bin/edgefdl"
}

# ------------------------------------------------------------------------------------ realstack
realstack_check() {
  if ! docker info >/dev/null 2>&1; then
    echo "ci.sh: realstack needs a working Docker daemon (docker info failed)" >&2
    return 1
  fi
  echo "realstack: kernel $(uname -r), docker $(docker version --format '{{.Server.Version}}')"
  scripts/lab.sh build
  scripts/lab.sh smoke
}

# -------------------------------------------------------------------------------------- release
version_check() {
  mkdir -p build/release
  "$py" - "$root" "${RELEASE_TAG:-}" <<'PY'
import os, re, sys
from pathlib import Path

root, tag = Path(sys.argv[1]), sys.argv[2]
meta = {}
exec((root / "edgefdl" / "__init__.py").read_text(encoding="utf-8"), meta)
v = meta["__version__"]
tag = tag or f"v{v}"
if tag != f"v{v}":
    raise SystemExit(f"version: tag {tag} but edgefdl.__version__ is {v} (expected tag v{v})")
cl = root / "CHANGELOG.md"
if not cl.is_file():
    raise SystemExit("version: CHANGELOG.md is missing")
text = cl.read_text(encoding="utf-8")
m = re.search(rf"^## \[v?{re.escape(v)}\][^\n]*$", text, re.M)
if not m:
    raise SystemExit(f"version: CHANGELOG.md has no section '## [{v}] - YYYY-MM-DD'")
heading, rest = m.group(0), text[m.end():]
end = re.search(r"^## \[", rest, re.M)
body = rest[:end.start() if end else len(rest)]
# Link reference definitions at the end of the file ("[0.1.0]: https://...") are not notes.
body = "\n".join(ln for ln in body.splitlines() if not re.match(r"^\[[^\]]+\]:\s*\S+$", ln)).strip("\n")
if not re.search(r"\d{4}-\d{2}-\d{2}", heading):
    raise SystemExit(f"version: CHANGELOG.md section {heading!r} has no date")
if re.search(r"\{\{|\bTODO\b|\bTBD\b", heading + body):
    raise SystemExit(f"version: CHANGELOG.md section {heading!r} still holds a placeholder")
if not body.strip():
    raise SystemExit(f"version: CHANGELOG.md section {heading!r} is empty")

repo = "https://github.com/seuclkzsd/EdgeFDL"
# Relative links of the CHANGELOG as links into the tagged tree: on the release page a relative
# link would resolve against .../releases/tag/.
body = re.sub(r"\]\((?!https?:|mailto:|#)(?:\./)?([^)\s]+)\)", lambda x: f"]({repo}/blob/{tag}/{x.group(1)})", body)
dl = f"{repo}/releases/download/{tag}"
notes = f"""{body}

## Assets

| File | Contents |
|---|---|
| `edgefdl-{v}-py3-none-any.whl`, `edgefdl-{v}.tar.gz` | the Python package (wheel and source distribution) |
| `edgefdl-demo-model.pt` | the released model bundle for the demo topology (`results/model.pt`: weights, topology, baseline, measurement plan) |
| `edgefdl-{v}-results.tar.gz` | `results/` of this release: model bundle, evaluation, self-test and robustness results, pipeline log, real-stack smoke logs |
| `SHA256SUMS` | SHA-256 checksums of the files above |

Check the downloads with `sha256sum -c SHA256SUMS` (add `--ignore-missing` to check only the files you downloaded).

## Try it

```bash
curl -LO {dl}/edgefdl-{v}-py3-none-any.whl
curl -LO {dl}/edgefdl-demo-model.pt
pip install torch --index-url https://download.pytorch.org/whl/cpu   # the CPU build is enough
pip install edgefdl-{v}-py3-none-any.whl
edgefdl serve --model edgefdl-demo-model.pt --backend sim --interval 2   # dashboard: http://127.0.0.1:8460
```

Documentation: {repo}/blob/{tag}/README.md · Results and how they were measured: {repo}/blob/{tag}/docs/results.md
"""
out = root / "build" / "release" / "notes.md"
out.write_text(notes, encoding="utf-8")
print(f"version: tag {tag} = edgefdl {v}; CHANGELOG section {heading!r}; notes in {out.relative_to(root)}")
gh = os.environ.get("GITHUB_OUTPUT")
if gh:
    with open(gh, "a", encoding="utf-8") as f:
        f.write(f"version={v}\ntag={tag}\n")
PY
}

release_assets() {
  local v out epoch
  v="$("$py" -c 'import edgefdl; print(edgefdl.__version__)')"
  out=build/release/assets
  rm -rf "$out" && mkdir -p "$out"
  cp "dist/edgefdl-$v-py3-none-any.whl" "dist/edgefdl-$v.tar.gz" "$out/"
  cp results/model.pt "$out/edgefdl-demo-model.pt"
  # results/ as published: the files git sees (tracked or not ignored), byte-for-byte reproducible
  # for a given commit (sorted names, commit time, no owner, gzip without a time stamp).
  epoch="$(git log -1 --format=%ct 2>/dev/null || date +%s)"
  { git ls-files --cached --others --exclude-standard -- results 2>/dev/null || find results -type f; } \
    | LC_ALL=C sort > build/release/results.list
  [ -s build/release/results.list ] || find results -type f | LC_ALL=C sort > build/release/results.list
  tar --sort=name --mtime="@$epoch" --owner=0 --group=0 --numeric-owner --mode='u+rw,go+r,go-w' \
    -cf - -T build/release/results.list | gzip -n -9 > "$out/edgefdl-$v-results.tar.gz"
  (cd "$out" && sha256sum -- * > SHA256SUMS && cat SHA256SUMS && sha256sum -c --quiet SHA256SUMS)
  echo "release: $(wc -l < build/release/results.list) files of results/ in edgefdl-$v-results.tar.gz;" \
    "assets in $out"
}

for s in "${steps[@]}"; do
  case "$s" in
    licenses) step licenses "$py" tools/dev/spdx.py ;;
    tests) step tests tests_check ;;
    serve) step serve serve_check "$tmp/serve" "$root" "$py" -m edgefdl ;;
    pipeline) step pipeline pipeline_check ;;
    dist) step dist dist_check ;;
    realstack) step realstack realstack_check ;;
    version) step version version_check ;;
    release) step version version_check; step dist dist_check; step release release_assets ;;
  esac
done
echo "ci: all steps passed (${steps[*]})"

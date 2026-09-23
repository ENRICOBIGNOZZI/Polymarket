#!/usr/bin/env bash
# Exact-SHA trigger: latency program final validation.
set -euo pipefail

APP_DIR="${POLYMARKET_APP_DIR:-$HOME/polymarket}"
SHA=""
SAMPLES=1000
OUT_DIR=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sha) SHA="$2"; shift 2;;
    --samples) SAMPLES="$2"; shift 2;;
    --output-dir) OUT_DIR="$2"; shift 2;;
    *) echo "unknown argument: $1" >&2; exit 64;;
  esac
done

[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "exact SHA required" >&2; exit 64; }
[[ "$SAMPLES" =~ ^[1-9][0-9]*$ ]] || { echo "positive samples required" >&2; exit 64; }
(( SAMPLES >= 100 && SAMPLES <= 20000 )) || { echo "samples out of range" >&2; exit 64; }
[[ -d "$APP_DIR/.git" ]] || { echo "missing repo $APP_DIR" >&2; exit 2; }

CACHE="$HOME/.cache/polymarket-v7-latency-lab"
OUT_DIR="${OUT_DIR:-$CACHE/results/$SHA}"
WORK="$CACHE/worktree-$SHA-$$"
mkdir -p "$CACHE/results" "$OUT_DIR"
cleanup(){
  git -C "$APP_DIR" worktree remove --force "$WORK" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

if ! git -C "$APP_DIR" cat-file -e "$SHA^{commit}" 2>/dev/null; then
  git -C "$APP_DIR" fetch --quiet origin "$SHA"
fi
test "$(git -C "$APP_DIR" rev-parse "$SHA")" = "$SHA"
git -C "$APP_DIR" worktree add --detach "$WORK" "$SHA" >/dev/null
test "$(git -C "$WORK" rev-parse HEAD)" = "$SHA"

python3 - "$OUT_DIR/host.json" "$SHA" <<'PY'
import json,os,platform,subprocess,sys
from pathlib import Path
def read(p):
    try:return Path(p).read_text().strip()
    except Exception:return None
def cmd(*args):
    try:return subprocess.check_output(args,text=True,stderr=subprocess.DEVNULL).strip()
    except Exception:return None
out={
 "schema":"polymarket_v7_london_latency_host_v1",
 "sha":sys.argv[2],
 "hostname":platform.node(),
 "kernel":platform.release(),
 "machine":platform.machine(),
 "cpu_count":os.cpu_count(),
 "cpu_model":cmd("bash","-lc","lscpu | sed -n 's/^Model name:[[:space:]]*//p' | head -1"),
 "governor":read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"),
 "no_turbo":read("/sys/devices/system/cpu/intel_pstate/no_turbo"),
 "cmdline":read("/proc/cmdline"),
}
Path(sys.argv[1]).write_text(json.dumps(out,sort_keys=True)+"\n")
PY

build_one(){
  local name="$1" ipo="$2" extra_cxx="${3:-}" extra_link="${4:-}"
  local dir="$WORK/build-$name"
  cmake -S "$WORK" -B "$dir" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_TESTING=OFF \
    -DPM_LONDON_RUNTIME_ONLY=ON \
    -DPM_LONDON_LATENCY_BENCHMARKS=ON \
    -DPM_V7_ENABLE_IPO="$ipo" \
    -DCMAKE_CXX_FLAGS_RELEASE="-O3 -DNDEBUG $extra_cxx" \
    -DCMAKE_EXE_LINKER_FLAGS_RELEASE="$extra_link" >/dev/null
  cmake --build "$dir" --parallel "$(nproc)" --target \
    polymarket_v7_paired_sign_bench \
    polymarket_v7_pure_arb_handoff_bench \
    polymarket_v7_public_paired_clob_transport_probe \
    polymarket_v7_public_event_to_wire_probe >/dev/null
}

build_one noipo OFF
build_one ipo ON

PGO_DIR="$WORK/pgo-data"
mkdir -p "$PGO_DIR"
build_one pgo-generate OFF "-fprofile-generate=$PGO_DIR" "-fprofile-generate=$PGO_DIR"
"$WORK/build-pgo-generate/polymarket_v7_paired_sign_bench" --samples 5000 >/dev/null
build_one pgo-use OFF "-fprofile-use=$PGO_DIR -fprofile-correction" "-fprofile-use=$PGO_DIR -fprofile-correction"

run_sign(){
  local name="$1"
  "$WORK/build-$name/polymarket_v7_paired_sign_bench" --samples "$SAMPLES" \
    > "$OUT_DIR/sign-$name.json"
}
run_sign noipo
run_sign ipo
run_sign pgo-use

"$WORK/build-ipo/polymarket_v7_pure_arb_handoff_bench" --samples "$SAMPLES" \
  > "$OUT_DIR/handoff.json"

mapfile -t PM_EVENT_TOKENS < <(python3 - "$WORK" <<'PY'
import sys
from pathlib import Path
root=Path(sys.argv[1])
sys.path.insert(0,str(root/"scripts"))
from v7_public_book_wire_probe import discover_market
market=discover_market()
tokens=[str(x) for x in market.get("_tokens",[])]
if len(tokens)<2 or tokens[0]==tokens[1]:
    raise SystemExit("two distinct public market token ids required")
print(tokens[0]); print(tokens[1])
PY
)
[[ "${#PM_EVENT_TOKENS[@]}" == 2 ]]
EVENT_SAMPLES="$SAMPLES"
(( EVENT_SAMPLES > 250 )) && EVENT_SAMPLES=250
"$WORK/build-ipo/polymarket_v7_public_event_to_wire_probe" \
  --yes-token "${PM_EVENT_TOKENS[0]}" \
  --no-token "${PM_EVENT_TOKENS[1]}" \
  --samples "$EVENT_SAMPLES" --min-interval-ms 100 --timeout-seconds 180 \
  > "$OUT_DIR/event-to-wire.json"

install -m 0755 \
  "$WORK/build-ipo/polymarket_v7_public_paired_clob_transport_probe" \
  "$OUT_DIR/public-paired-clob-probe"

soft_before="$(awk '/^softirq /{print $2}' /proc/stat 2>/dev/null || echo 0)"
ctxt_before="$(awk '/^ctxt /{print $2}' /proc/stat 2>/dev/null || echo 0)"
drops_before="$(python3 - <<'PY'
from pathlib import Path
total=0
for line in Path('/proc/net/dev').read_text().splitlines()[2:]:
    _, raw=line.split(':',1); x=raw.split()
    total += int(x[3]) + int(x[11])
print(total)
PY
)"
perf_used=0
if command -v perf >/dev/null 2>&1 \
   && perf stat -x, -o "$OUT_DIR/perf-check.csv" -e task-clock true >/dev/null 2>&1; then
  set +e
  perf stat -x, -o "$OUT_DIR/perf-public.csv" \
    -e task-clock,context-switches,cpu-migrations \
    "$WORK/build-ipo/polymarket_v7_public_paired_clob_transport_probe" \
      --samples "$SAMPLES" --warmup 8 \
      > "$OUT_DIR/public-paired-clob.json"
  perf_rc=$?
  set -e
  if [[ "$perf_rc" == 0 ]]; then
    perf_used=1
  else
    rm -f "$OUT_DIR/perf-public.csv" "$OUT_DIR/public-paired-clob.json"
    "$WORK/build-ipo/polymarket_v7_public_paired_clob_transport_probe" \
      --samples "$SAMPLES" --warmup 8 > "$OUT_DIR/public-paired-clob.json"
  fi
else
  "$WORK/build-ipo/polymarket_v7_public_paired_clob_transport_probe" \
    --samples "$SAMPLES" --warmup 8 > "$OUT_DIR/public-paired-clob.json"
fi
soft_after="$(awk '/^softirq /{print $2}' /proc/stat 2>/dev/null || echo 0)"
ctxt_after="$(awk '/^ctxt /{print $2}' /proc/stat 2>/dev/null || echo 0)"
drops_after="$(python3 - <<'PY'
from pathlib import Path
total=0
for line in Path('/proc/net/dev').read_text().splitlines()[2:]:
    _, raw=line.split(':',1); x=raw.split()
    total += int(x[3]) + int(x[11])
print(total)
PY
)"

python3 - "$OUT_DIR" "$SHA" "$soft_before" "$soft_after" "$ctxt_before" "$ctxt_after" "$drops_before" "$drops_after" "$perf_used" <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1]); sha=sys.argv[2]
def load(name):return json.loads((root/name).read_text())
base=load("sign-noipo.json"); ipo=load("sign-ipo.json"); pgo=load("sign-pgo-use.json")
handoff=load("handoff.json"); net=load("public-paired-clob.json")
reaction=load("event-to-wire.json")
def dist5(x):
    return {"p50":x["p50"],"p95":x["p95"],"p99":x["p99"],"p999":x["p999"],"max":x["max"]}
def sign(d):
    return dist5(d["latency_ns"]["parallel_pair_completion"])
def candidate(test,base):
    return test["p99"] <= base["p99"]*.90 and test["p999"] <= base["p999"]
def pair_mode(d):
    latency=d["latency_ns"]
    serial=dist5(latency["serial_pair_completion"])
    parallel=dist5(latency["parallel_pair_completion"])
    serial_skew=dist5(latency["serial_leg_completion_skew"])
    parallel_skew=dist5(latency["parallel_leg_completion_skew"])
    return {
      "serial":serial,
      "parallel":parallel,
      "serial_leg_completion_skew":serial_skew,
      "parallel_leg_completion_skew":parallel_skew,
      "parallel_p99_improvement_pct":
        100*(serial["p99"]-parallel["p99"])/serial["p99"] if serial["p99"] else None,
      "parallel_p999_improvement_pct":
        100*(serial["p999"]-parallel["p999"])/serial["p999"] if serial["p999"] else None,
      "parallel_promotion_candidate":candidate(parallel,serial),
    }
b=sign(base); i=sign(ipo); p=sign(pgo)
bm=pair_mode(base); im=pair_mode(ipo); pm=pair_mode(pgo)
perf={}
if sys.argv[9]=="1":
    path=root/"perf-public.csv"
    if path.exists():
        for raw in path.read_text().splitlines():
            if not raw or raw.startswith('#'): continue
            fields=raw.split(',')
            if len(fields)<3: continue
            value=fields[0].strip().replace('<not counted>','').replace('<not supported>','')
            event=fields[2].strip()
            try: perf[event]=float(value)
            except Exception: pass
summary={
 "schema":"polymarket_v7_london_latency_lab_v1",
 "sha":sha,
 "paper_only":True,
 "authenticated_execution":False,
 "real_order_submission":False,
 "signing":{
   "noipo":b,"ipo":i,"pgo":p,
   "ipo_p99_improvement_pct":100*(b["p99"]-i["p99"])/b["p99"] if b["p99"] else None,
   "pgo_p99_improvement_pct":100*(b["p99"]-p["p99"])/b["p99"] if b["p99"] else None,
   "ipo_promotion_candidate":candidate(i,b),
   "pgo_promotion_candidate":candidate(p,b),
 },
 "signing_pair_mode":{
   "noipo":bm,
   "ipo":im,
   "pgo":pm,
 },
 "handoff":handoff["latency_ns"],
 "direct_decision_queue_depth":0,
 "event_to_public_wire":reaction,
 "public_transport":net,
 "host_counters_delta":{
   "softirq":max(0,int(sys.argv[4])-int(sys.argv[3])),
   "context_switches":max(0,int(sys.argv[6])-int(sys.argv[5])),
   "network_drops":max(0,int(sys.argv[8])-int(sys.argv[7])),
   "perf":perf,
 },
}
(root/"summary.json").write_text(json.dumps(summary,sort_keys=True)+"\n")
print(json.dumps(summary,sort_keys=True))
PY

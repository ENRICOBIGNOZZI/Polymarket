#!/usr/bin/env bash
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
    polymarket_v7_public_paired_tls_probe >/dev/null
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

soft_before="$(awk '/^softirq /{print $2}' /proc/stat 2>/dev/null || echo 0)"
ctxt_before="$(awk '/^ctxt /{print $2}' /proc/stat 2>/dev/null || echo 0)"
"$WORK/build-ipo/polymarket_v7_public_paired_tls_probe" \
  --samples "$SAMPLES" --warmup 8 > "$OUT_DIR/public-paired-tls.json"
soft_after="$(awk '/^softirq /{print $2}' /proc/stat 2>/dev/null || echo 0)"
ctxt_after="$(awk '/^ctxt /{print $2}' /proc/stat 2>/dev/null || echo 0)"

python3 - "$OUT_DIR" "$SHA" "$soft_before" "$soft_after" "$ctxt_before" "$ctxt_after" <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1]); sha=sys.argv[2]
def load(name):return json.loads((root/name).read_text())
base=load("sign-noipo.json"); ipo=load("sign-ipo.json"); pgo=load("sign-pgo-use.json")
handoff=load("handoff.json"); net=load("public-paired-tls.json")
def sign(d):
    x=d["latency_ns"]["parallel_pair_completion"]
    return {"p50":x["p50"],"p95":x["p95"],"p99":x["p99"],"p999":x["p999"],"max":x["max"]}
def candidate(test,base):
    return test["p99"] <= base["p99"]*.90 and test["p999"] <= base["p999"]
b=sign(base); i=sign(ipo); p=sign(pgo)
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
 "handoff":handoff["latency_ns"],
 "public_transport":net,
 "host_counters_delta":{
   "softirq":max(0,int(sys.argv[4])-int(sys.argv[3])),
   "context_switches":max(0,int(sys.argv[6])-int(sys.argv[5])),
 },
}
(root/"summary.json").write_text(json.dumps(summary,sort_keys=True)+"\n")
print(json.dumps(summary,sort_keys=True))
PY

"""Zurich-midnight cumulative research job; catch-up, receipts, no promotion."""
from __future__ import annotations
import argparse
from collections import defaultdict
from datetime import datetime, time as day_time, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time
from zoneinfo import ZoneInfo
from .common import ROOT, SAFETY, canonical, digest, immutable, publish, read_json, research_host
from .catalog import inventory
from .dataset import materialize
from .registry import transition


def midnight(now=None):
    now = datetime.now(timezone.utc) if now is None else now
    if now.tzinfo is None:
        raise ValueError("AWARE_TIME_REQUIRED")
    local = now.astimezone(ZoneInfo("Europe/Zurich"))
    cut = datetime.combine(local.date(), day_time(), ZoneInfo("Europe/Zurich"))
    return local.date().isoformat(), int(cut.timestamp())*1_000_000_000


def growth_receipt(root, previous, dataset, rows):
    """Compare compact unique-signal history, including pending-to-resolved changes."""
    old=[]
    if previous and previous.get('dataset_sha256'):
        path=root/'dataset_manifests'/(previous['dataset_sha256']+'.json')
        if not path.exists(): raise ValueError('PREVIOUS_CUMULATIVE_DATASET_MANIFEST_MISSING')
        if path.exists():
            manifest=read_json(path);view=manifest['views']['signals']['sha256']
            with (root/'datasets'/(view+'.jsonl')).open() as stream:
                old=[json.loads(line) for line in stream if line.strip()]
    training=[r for r in rows if r.get('training_eligible') and r.get('outcome') is not None]
    old_training=[r for r in old if r.get('training_eligible') and r.get('outcome') is not None]
    ids={r['signal_id'] for r in rows};old_ids={r['signal_id'] for r in old}
    labels={r['signal_id'] for r in training};old_labels={r['signal_id'] for r in old_training}
    markets={r['market_id'] for r in training};old_markets={r['market_id'] for r in old_training}
    lost=old_ids-ids;lost_labels=old_labels-labels
    return dict(previous_training_rows=len(old_training),training_rows=len(training),
        new_observations=len(ids-old_ids),new_resolved_observations=len(labels-old_labels),
        new_labeled_markets=len(markets-old_markets),total_unique_signals=len(ids),
        total_unique_markets=dataset['unique_markets'],unresolved_observations=dataset['unresolved'],
        first_training_timestamp_ns=min((r['decision_ns'] for r in training),default=None),
        last_training_timestamp_ns=max((r['decision_ns'] for r in training),default=None),
        first_observation_timestamp_ns=dataset['first_decision_ns'],last_observation_timestamp_ns=dataset['last_decision_ns'],
        historical_data_shrank=bool(lost or lost_labels),lost_observations=len(lost),lost_training_observations=len(lost_labels),
        history_policy='EXPANDING_ALL_VALID_HISTORY')


def run(root, *, now=None, train_fn=None):
    root = research_host(root)
    date, cutoff = midnight(now); started = time.monotonic()
    with (root/".daily.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        config = read_json(root/"settings.json")
        refresh = refresh_inputs(root, config)
        receipt_path = root/"receipts"/(date+".json")
        if receipt_path.exists():
            return read_json(receipt_path)
        prior = [read_json(p) for p in sorted((root/"receipts").glob("*.json"))]
        previous = prior[-1] if prior else None
        code_sha = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
        implementation = {str(p.relative_to(ROOT)): digest(p.read_bytes())
                          for p in sorted((ROOT/"research/learning").glob("*.py"))}
        dirty = bool(subprocess.check_output(["git", "-C", str(ROOT), "status", "--porcelain"], text=True).strip())
        try:
            roots = list(config["source_roots"])
            labels_root = root/"public_settlements"
            if labels_root.exists():
                roots.append(str(labels_root))
            catalog = inventory(roots, root)
            revisions = set(catalog["source_revisions"])
            for p in (root/"catalog_manifests").glob("*.json"):
                revisions.update(read_json(p).get("source_revisions", []))
            dataset, rows = materialize(root/"store", sorted(revisions), root, cutoff, code_sha,
                                        execution_scenario=config.get("execution_scenario"))
            growth = growth_receipt(root, previous, dataset, rows)
            same = previous and previous.get("training_information_sha256") == dataset["training_information_sha256"]
            reports = []; candidate_sha = previous.get("candidate_sha256") if previous else None
            if refresh.get('source_failures'):
                result = 'SOURCE_COLLECTION_INCOMPLETE_NO_FIT'
            elif growth["historical_data_shrank"]:
                result = "DATASET_SHRINKAGE_REVIEW_REQUIRED"
            elif same:
                result = "NO_NEW_TRAINING_INFORMATION"
            else:
                if train_fn is None:
                    from .train import train_stratum
                    train_fn = train_stratum
                strata = defaultdict(list)
                for row in rows:
                    strata[row["stratum"]].append(row)
                result = "INSUFFICIENT_DATA" if not strata else "CANDIDATES_RECORDED_NO_AUTO_PROMOTION"
                for name, subset in sorted(strata.items()):
                    report = train_fn(subset, dataset, root)
                    reports.append({"stratum": name, "report_sha256": report["report_sha256"],
                                    "state": report["state"], "reasons": report["reasons"],
                                    "candidate_sha256": report.get("candidate_sha256")})
                    sha = report.get("candidate_sha256")
                    if sha:
                        transition(root, sha, "TRAINED")
                        if report["state"] == "REJECTED":
                            transition(root, sha, "REJECTED", evidence={"reasons": report["reasons"]})
                        candidate_sha = sha
                if reports and not any(r.get("candidate_sha256") for r in reports):
                    result = "REPORTS_RECORDED_NO_ELIGIBLE_CANDIDATE"
            settlement_status = refresh.get('public_settlements')
            receipt = {"schema": "v7_daily_retraining_receipt_v1", **SAFETY, "training_date": date,
                       "cutoff_ns": cutoff, "timezone": "Europe/Zurich", "code_sha": code_sha,
                       "cutoff_utc": datetime.fromtimestamp(cutoff/1e9, timezone.utc).isoformat(),
                       "cutoff_local": datetime.fromtimestamp(cutoff/1e9, ZoneInfo("Europe/Zurich")).isoformat(),
                       **growth,
                       "implementation_sha256": digest(canonical(implementation)), "dirty_worktree": dirty,
                       "catalog_sha256": catalog["manifest_sha256"], "dataset_sha256": dataset["dataset_sha256"],
                       "training_information_sha256": dataset["training_information_sha256"],
                       "candidate_sha256": candidate_sha, "artifact_sha256": None,
                       "candidate_state": "REJECTED" if reports else "UNCHANGED" if candidate_sha else "NOT_TRAINED",
                       "previous_candidate_sha": previous.get("candidate_sha256") if previous else None,
                       "rows": dataset["signal_rows"], "markets": dataset["unique_markets"],
                       "new_rows": dataset["signal_rows"]-(previous.get("rows", 0) if previous else 0),
                       "new_markets": dataset["unique_markets"]-(previous.get("markets", 0) if previous else 0),
                       "duration_seconds": round(time.monotonic()-started, 6), "result": result,
                       "reports": reports, "catalog_errors": catalog["errors"],
                       "new_public_settlements": settlement_status}
            immutable(receipt_path, canonical(receipt))
            temp = root/("status.tmp."+str(os.getpid()))
            temp.write_bytes(canonical(receipt)); temp.replace(root/"status.json")
            return receipt
        except Exception as exc:
            publish(root, "failures", {"schema": "v7_retraining_failure_v1", **SAFETY,
                    "training_date": date, "cutoff_ns": cutoff, "code_sha": code_sha,
                    "error_type": type(exc).__name__, "reason": str(exc)})
            raise


def refresh_inputs(root, config):
    """Poll independently of the midnight receipt; never backdate a new label.

    A label first received after midnight belongs to the following cutoff even
    if the market settled earlier. Completed daily jobs still collect labels.
    """
    path=root/'collection_status.json'
    old=read_json(path) if path.exists() else {}
    interval=int(config.get('collection_interval_seconds',300))
    if time.time_ns()-old.get('information_ns',0) < interval*1_000_000_000:
        return old
    if config.get('sync_london') is True:
        subprocess.run([str(ROOT/'research/pull_london_evidence.sh')],cwd=ROOT,check=True,
                       timeout=1800,stdout=subprocess.DEVNULL)
    markets=set();source_failures=[]
    # The collector publishes this small, atomic inventory from its cumulative
    # opportunity index. No need to rescan all historical raw tapes every wake.
    for source in config['source_roots']:
        p=Path(source)/'population.json'
        if p.exists():
            population=read_json(p);markets.update(map(str,population.get('markets',[])))
            source_failures.extend(population.get('capture_failures',[]))
            if time.time_ns()-population.get('updated_ns',0)>900*1_000_000_000:
                source_failures.append({'source':str(p),'reason':'STALE_RESEARCH_COLLECTION'})
        elif config.get('require_hft_population'):
            source_failures.append({'source':str(p),'reason':'MISSING_HFT_POPULATION'})
    status={'information_ns':time.time_ns(),'markets':len(markets),'public_settlements':None,
            'source_failures':source_failures}
    if config.get('fetch_public_settlements') is True:
        from .settlements import collect
        # Existing compact datasets remain an input during collector migration.
        prior=sorted((root/'receipts').glob('*.json'))
        if prior:
            receipt=read_json(prior[-1]); manifest=root/'dataset_manifests'/(receipt['dataset_sha256']+'.json')
            if manifest.exists():
                view=read_json(manifest)['views']['signals']['sha256']
                with (root/'datasets'/(view+'.jsonl')).open() as stream:
                    markets.update(str(r['market_id']) for line in stream if (r:=json.loads(line)).get('outcome') is None)
        status['public_settlements']=collect(markets,root/'public_settlements')
    temporary=path.with_suffix('.tmp');temporary.write_bytes(canonical(status));temporary.replace(path)
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run(args.root)
    except BlockingIOError:
        print('{"state":"ALREADY_RUNNING"}'); return
    print(json.dumps({k: result[k] for k in ("training_date", "result", "dataset_sha256", "candidate_state")}))


if __name__ == "__main__":
    main()

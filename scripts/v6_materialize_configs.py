#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    ap=argparse.ArgumentParser();ap.add_argument("--config",type=Path,default=Path("config/paper_v6.json"));ap.add_argument("--run-root",type=Path,required=True);args=ap.parse_args()
    cfg=json.loads(args.config.read_text(encoding="utf-8"));v=cfg["v6"];total=float(cfg["starting_capital"])
    if cfg.get("multi_strategy",{}).get("paper_only") is not True or v.get("paper_only") is not True:raise SystemExit("V6 must remain paper-only")
    alloc=[("maker",v["micro_maker_capital_fraction"]),("micro_taker",v["micro_taker_capital_fraction"]),("broker",v["relative_value_capital_fraction"]),("hard_arb",v["hard_arb_capital_fraction"]),("external",v["external_capital_fraction"])]
    if abs(sum(float(frac) for _,frac in alloc)+float(v["reserve_fraction"])-1.0)>1e-9:raise SystemExit("V6 sleeve fractions plus reserve must equal one")
    root=args.run_root;root.mkdir(parents=True,exist_ok=True)
    manifest={"schema":"polymarket_v6_sleeves_v1","paper_only":True,"starting_capital":total,"reserve_fraction":float(v["reserve_fraction"]),"sleeves":[]}
    for name,frac in alloc:
        child={k:x for k,x in cfg.items() if k not in {"v6","multi_strategy"}};child["starting_capital"]=total*float(frac);child["run_dir"]=str(root/name);child["expert_weights"]={"micro":0.0,"pca":0.0,"graph":0.0,"semantic":0.0,"external":0.0}
        if name=="external":child["external_signals_file"]=str(root/"external_signals.csv");child["expert_weights"]["external"]=1.0;child["uncertainty_penalty"]=0.0
        path=root/f"{name}_config.json";path.write_text(json.dumps(child,indent=2,sort_keys=True)+"\n",encoding="utf-8");manifest["sleeves"].append({"name":name,"capital_fraction":float(frac),"starting_capital":child["starting_capital"],"config":str(path)})
    (root/"v6_sleeves.json").write_text(json.dumps(manifest,indent=2,sort_keys=True)+"\n",encoding="utf-8");print(json.dumps(manifest,sort_keys=True));return 0


if __name__=="__main__":raise SystemExit(main())

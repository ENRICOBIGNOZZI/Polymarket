"""Render exhaustive native 2H alpha equity galleries from compressed raw paths.

Research-only visualization. All PnL was computed on London causal evidence;
this module only renders the already-computed paths on the GitHub runner.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
from collections import defaultdict
from pathlib import Path
import re

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

LATENCIES=(5,10,25,50,100,250)
EXITS=(500,750,1000,1500,2000,3000,4000,5000,7500,10000)


def safe(value):
    return re.sub(r"[^A-Za-z0-9_.-]+","_",str(value))


def load_paths(root):
    out=defaultdict(lambda:defaultdict(list))
    path=root/"22_native_alpha_equity_paths.csv.gz"
    with gzip.open(path,"rt",newline="",encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            out[row["alpha"]][row["cell"]].append({
                "decision_ns":int(row["decision_ns"]),
                "cumulative_pnl":float(row["cumulative_pnl"]),
            })
    for by_cell in out.values():
        for rows in by_cell.values():
            rows.sort(key=lambda r:r["decision_ns"])
    return out


def equity(rows):
    if not rows:return [],[]
    origin=rows[0]["decision_ns"]
    return (
        [(r["decision_ns"]-origin)/60_000_000_000 for r in rows],
        [r["cumulative_pnl"] for r in rows],
    )


def save(path):
    path.parent.mkdir(parents=True,exist_ok=True)
    plt.tight_layout()
    plt.savefig(path,dpi=115,bbox_inches="tight")
    plt.close()


def plot_lines(path,title,event_map,keys,legend=True):
    plt.figure(figsize=(11,5.5))
    drawn=False
    for key in keys:
        x,y=equity(event_map.get(key,[]))
        if not x:continue
        plt.plot(x,y,label=key,linewidth=.95)
        drawn=True
    if drawn:
        plt.axhline(0,linewidth=.7)
        plt.xlabel("minutes since first fill")
        plt.ylabel("cumulative PnL")
        if legend:plt.legend(fontsize=6,ncol=3)
    else:
        plt.text(.5,.5,"No observed fills",ha="center",va="center",transform=plt.gca().transAxes)
        plt.xticks([]);plt.yticks([])
    plt.title(title)
    save(path)


def heatmap(path,title,cells):
    matrix=[]
    for latency in LATENCIES:
        row=[]
        for horizon in EXITS:
            value=(cells.get(f"{latency}::{horizon}") or {}).get("total_pnl")
            row.append(float(value) if isinstance(value,(int,float)) else np.nan)
        matrix.append(row)
    plt.figure(figsize=(11,4.5))
    image=plt.imshow(np.asarray(matrix),aspect="auto",origin="lower")
    plt.colorbar(image,label="PnL")
    plt.xticks(range(len(EXITS)),[str(v) for v in EXITS],rotation=60,ha="right")
    plt.yticks(range(len(LATENCIES)),[str(v) for v in LATENCIES])
    plt.xlabel("exit horizon ms")
    plt.ylabel("entry latency ms")
    plt.title(title)
    save(path)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root",type=Path,required=True)
    a=p.parse_args(argv)
    root=a.root
    data=json.loads((root/"20_native_alpha_library.json").read_text(encoding="utf-8"))
    alphas=data.get("alphas") or {}
    paths=load_paths(root)
    gallery=root/"native_alpha_equity_gallery"
    gallery.mkdir(parents=True,exist_ok=True)
    figures=[]

    for alpha_name,alpha in sorted(alphas.items()):
        event_map=paths.get(alpha_name,{})
        stem=safe(alpha_name)

        rel=f"native_alpha_equity_gallery/{stem}/00_all_60_cells.png"
        plot_lines(root/rel,f"{alpha_name}: all 60 equity lines",event_map,
                   [f"{l}::{h}" for l in LATENCIES for h in EXITS],legend=False)
        figures.append(rel)

        rel=f"native_alpha_equity_gallery/{stem}/01_pnl_heatmap.png"
        heatmap(root/rel,f"{alpha_name}: entry × exit PnL",alpha.get("cells") or {})
        figures.append(rel)

        for latency in LATENCIES:
            rel=f"native_alpha_equity_gallery/{stem}/latency_{latency}ms_all_exits.png"
            plot_lines(root/rel,f"{alpha_name}: entry {latency}ms, all exits",event_map,
                       [f"{latency}::{h}" for h in EXITS],legend=True)
            figures.append(rel)

    # At each latency × exit cell, compare every alpha's equity over time.
    for latency in LATENCIES:
        for horizon in EXITS:
            key=f"{latency}::{horizon}"
            rel=f"native_alpha_equity_gallery/cross_alpha/cell_{latency}ms_{horizon}ms.png"
            plt.figure(figsize=(12,6))
            drawn=False
            for alpha_name in sorted(alphas):
                x,y=equity(paths.get(alpha_name,{}).get(key,[]))
                if not x:continue
                plt.plot(x,y,label=alpha_name,linewidth=.8)
                drawn=True
            if drawn:
                plt.axhline(0,linewidth=.7)
                plt.xlabel("minutes since first fill")
                plt.ylabel("cumulative PnL")
                plt.legend(fontsize=4.8,ncol=4)
            else:
                plt.text(.5,.5,"No observed fills",ha="center",va="center",transform=plt.gca().transAxes)
                plt.xticks([]);plt.yticks([])
            plt.title(f"All alphas: entry {latency}ms, exit {horizon}ms")
            save(root/rel)
            figures.append(rel)

    manifest={
        "schema":"polymarket_v7_native_2h_alpha_gallery_v2",
        "research_only":True,
        "source_paths":"22_native_alpha_equity_paths.csv.gz",
        "alpha_count":len(alphas),
        "cells_per_alpha":len(LATENCIES)*len(EXITS),
        "latencies_ms":list(LATENCIES),
        "exit_horizons_ms":list(EXITS),
        "per_alpha_figures":2+len(LATENCIES),
        "cross_alpha_figures":len(LATENCIES)*len(EXITS),
        "figure_count":len(figures),
        "figures":figures,
        "grid_csv":"21_native_alpha_grid.csv",
    }
    (gallery/"gallery_manifest.json").write_text(
        json.dumps(manifest,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    print(json.dumps(manifest,sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())

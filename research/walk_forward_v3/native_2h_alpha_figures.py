"""Render exhaustive native 2H alpha equity galleries.

Research-only visualization. Reads 20_native_alpha_library.json and writes
PNG galleries plus compact CSV summaries. No execution authority.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

import matplotlib.pyplot as plt
import numpy as np

LATENCIES=(5,10,25,50,100,250)
EXITS=(500,750,1000,1500,2000,3000,4000,5000,7500,10000)


def safe(value):
    return re.sub(r"[^A-Za-z0-9_.-]+","_",str(value))


def equity(events):
    total=0.0
    xs=[];ys=[]
    for row in sorted(events,key=lambda r:(int(r["decision_ns"]),str(r["decision_id"]))):
        total+=float(row["cash_pnl"])
        xs.append(int(row["decision_ns"]))
        ys.append(total)
    return xs,ys


def save(path):
    plt.tight_layout()
    plt.savefig(path,dpi=150,bbox_inches="tight")
    plt.close()


def plot_lines(path,title,event_map,keys):
    plt.figure(figsize=(11,5.5))
    drawn=False
    for key in keys:
        x,y=equity(event_map.get(key,[]))
        if not x:
            continue
        origin=min(x)
        xm=[(v-origin)/60_000_000_000 for v in x]
        plt.plot(xm,y,label=key,linewidth=1.0)
        drawn=True
    if drawn:
        plt.axhline(0,linewidth=.8)
        plt.xlabel("minutes since first fill")
        plt.ylabel("cumulative PnL")
        plt.legend(fontsize=6,ncol=3)
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
    gallery=root/"native_alpha_equity_gallery"
    gallery.mkdir(parents=True,exist_ok=True)
    figures=[]

    for alpha_name,alpha in sorted(alphas.items()):
        d=gallery/safe(alpha_name)
        d.mkdir(parents=True,exist_ok=True)
        events=alpha.get("events") or {}
        cells=alpha.get("cells") or {}

        rel=f"native_alpha_equity_gallery/{safe(alpha_name)}/00_all_60_cells.png"
        plot_lines(root/rel,f"{alpha_name}: all 60 equity lines",events,
                   [f"{l}::{h}" for l in LATENCIES for h in EXITS])
        figures.append(rel)

        rel=f"native_alpha_equity_gallery/{safe(alpha_name)}/01_pnl_heatmap.png"
        heatmap(root/rel,f"{alpha_name}: entry × exit PnL",cells)
        figures.append(rel)

        for latency in LATENCIES:
            rel=f"native_alpha_equity_gallery/{safe(alpha_name)}/latency_{latency}ms_all_exits.png"
            plot_lines(root/rel,f"{alpha_name}: entry {latency}ms",events,
                       [f"{latency}::{h}" for h in EXITS])
            figures.append(rel)

        for horizon in EXITS:
            rel=f"native_alpha_equity_gallery/{safe(alpha_name)}/exit_{horizon}ms_all_latencies.png"
            plot_lines(root/rel,f"{alpha_name}: exit {horizon}ms",events,
                       [f"{l}::{horizon}" for l in LATENCIES])
            figures.append(rel)

    # Cross-alpha comparison at each latency/exit cell.
    compare=root/"native_alpha_equity_gallery"/"cross_alpha"
    compare.mkdir(parents=True,exist_ok=True)
    for latency in LATENCIES:
        for horizon in EXITS:
            key=f"{latency}::{horizon}"
            rel=f"native_alpha_equity_gallery/cross_alpha/cell_{latency}ms_{horizon}ms.png"
            plt.figure(figsize=(11,5.5))
            drawn=False
            for alpha_name,alpha in sorted(alphas.items()):
                x,y=equity((alpha.get("events") or {}).get(key,[]))
                if not x: continue
                origin=min(x)
                xm=[(v-origin)/60_000_000_000 for v in x]
                plt.plot(xm,y,label=alpha_name,linewidth=.9)
                drawn=True
            if drawn:
                plt.axhline(0,linewidth=.8)
                plt.xlabel("minutes since first fill")
                plt.ylabel("cumulative PnL")
                plt.legend(fontsize=5,ncol=3)
            else:
                plt.text(.5,.5,"No observed fills",ha="center",va="center",transform=plt.gca().transAxes)
                plt.xticks([]);plt.yticks([])
            plt.title(f"All alphas: entry {latency}ms, exit {horizon}ms")
            save(root/rel)
            figures.append(rel)

    manifest={
        "schema":"polymarket_v7_native_2h_alpha_gallery_v1",
        "research_only":True,
        "alpha_count":len(alphas),
        "cells_per_alpha":len(LATENCIES)*len(EXITS),
        "latencies_ms":list(LATENCIES),
        "exit_horizons_ms":list(EXITS),
        "per_alpha_figures":2+len(LATENCIES)+len(EXITS),
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

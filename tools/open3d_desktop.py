#!/usr/bin/env python3
"""Optional Open3D desktop player for a completed web-system job."""
from __future__ import annotations
import argparse, json, csv, time
from pathlib import Path
import numpy as np


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--axes',type=Path,required=True);ap.add_argument('--sequence',type=Path,required=True);ap.add_argument('--seconds-per-bar',type=float,default=.15)
    a=ap.parse_args()
    try: import open3d as o3d
    except ImportError as exc: raise SystemExit('请先安装 open3d: pip install open3d') from exc
    bars=json.loads(a.axes.read_text(encoding='utf-8'))['bars']; by={int(b['index']):b for b in bars}
    seq=[]
    with a.sequence.open('r',encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):seq.append(int(r['bar_index']))
    points=[];lines=[];line_end=[];off=0
    for idx in seq:
        P=np.asarray(by[idx]['axis'],float)/1000;points.extend(P.tolist())
        for i in range(len(P)-1):lines.append([off+i,off+i+1])
        off+=len(P);line_end.append(len(lines))
    points=np.asarray(points);lines=np.asarray(lines,np.int32)
    vis=o3d.visualization.Visualizer();vis.create_window('钢筋安装动画',1600,900)
    geom=o3d.geometry.LineSet();geom.points=o3d.utility.Vector3dVector(points);geom.lines=o3d.utility.Vector2iVector(np.empty((0,2),np.int32));geom.paint_uniform_color([.15,.55,1.0]);vis.add_geometry(geom)
    vis.get_render_option().background_color=np.array([.03,.04,.06])
    step=0;last=time.perf_counter()
    while vis.poll_events():
        now=time.perf_counter()
        if step<len(seq) and now-last>=a.seconds_per_bar:
            step+=1;last=now;geom.lines=o3d.utility.Vector2iVector(lines[:line_end[step-1]]);vis.update_geometry(geom)
            if step%100==0:print(f'{step}/{len(seq)}')
        vis.update_renderer()
    vis.destroy_window()
if __name__=='__main__':main()

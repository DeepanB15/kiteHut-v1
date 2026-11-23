"""Standalone, offline HTML graph visualization (``graph.html``).

* One self-contained file: inline CSS/JS/data, no network access (enforced by
  a restrictive Content-Security-Policy), no external libraries.
* Deterministic: the layout is computed here in Python (grid of files with
  symbols on rings around them), so the output is byte-stable for a graph.
* Safe: node labels come from source code, so the page renders them with
  ``textContent``/canvas text only, and the embedded JSON escapes ``<``.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Dict, List, Optional

from app.code_graph.models import CodeGraph, SymbolKind
from app.knowledge.constants import DEFAULT_MAX_VIZ_NODES, GENERATED_MARKER
from app.knowledge.index import GraphIndex, kind_label


@dataclass
class VisualizationOptions:
    max_nodes: int = DEFAULT_MAX_VIZ_NODES


def _viz_kind(kind: SymbolKind) -> str:
    if kind == SymbolKind.CLASS:
        return "class"
    if kind in (SymbolKind.METHOD, SymbolKind.ASYNC_METHOD):
        return "method"
    return "function"


def build_viz_data(graph: CodeGraph, options: Optional[VisualizationOptions] = None) -> dict:
    """Return the node/edge payload embedded in the page (also used by tests)."""
    opts = options or VisualizationOptions()
    idx = GraphIndex.from_graph(graph)
    files = sorted(graph.files, key=lambda f: f.relative_path)

    # Bound the drawing: all files first (capped), then classes, functions, methods.
    budget = max(0, opts.max_nodes)
    shown_files = files[:budget]
    budget -= len(shown_files)
    shown_ids = {f.id for f in shown_files}
    priority = {"class": 0, "function": 1, "method": 2}
    symbols = [s for s in idx.declared_symbols() if s.file_id in shown_ids]
    symbols.sort(key=lambda s: (priority[_viz_kind(s.kind)], -idx.incoming_calls(s.id), s.relative_path, s.start_line))
    shown_symbols = symbols[:budget]
    keep_syms = {s.id for s in shown_symbols}

    per_file: Dict[str, List] = {f.id: [] for f in shown_files}
    for s in sorted(shown_symbols, key=lambda s: (s.start_line, s.qualified_name)):
        per_file[s.file_id].append(s)

    # Layout: grid of file cells; symbols on concentric rings around each file.
    def ring_radius(n: int) -> float:
        radius, placed, ring = 0.0, 0, 0
        while placed < n:
            ring += 1
            radius = 16.0 + 13.0 * ring
            placed += 7 + 5 * ring
        return radius

    max_r = max([ring_radius(len(v)) for v in per_file.values()] or [0.0])
    cell = 2 * max_r + 50.0
    cols = max(1, math.ceil(math.sqrt(max(1, len(shown_files)))))

    nodes: List[dict] = []
    node_index: Dict[str, int] = {}
    for i, f in enumerate(shown_files):
        cx, cy = (i % cols) * cell, (i // cols) * cell
        node_index[f.id] = len(nodes)
        nodes.append({"k": "file", "n": f.relative_path.rsplit("/", 1)[-1], "q": f.relative_path,
                      "p": f.relative_path, "l": 1, "e": max(1, f.line_count), "x": round(cx, 1), "y": round(cy, 1), "r": 9})
        syms = per_file[f.id]
        pos, ring, k = 0, 1, 0
        while pos < len(syms):
            cap = 7 + 5 * ring
            batch = syms[pos:pos + cap]
            radius = 16.0 + 13.0 * ring
            for j, s in enumerate(batch):
                ang = 2 * math.pi * j / len(batch) + 0.35 * ring
                node_index[s.id] = len(nodes)
                nodes.append({"k": _viz_kind(s.kind), "n": s.name, "q": s.qualified_name, "p": s.relative_path,
                              "l": s.start_line, "e": s.end_line,
                              "x": round(cx + radius * math.cos(ang), 1), "y": round(cy + radius * math.sin(ang), 1), "r": 4})
            pos += cap
            ring += 1

    edges: List[dict] = []
    for f in shown_files:
        for tgt in idx.imports_out.get(f.id, ()):
            if tgt.id in node_index:
                edges.append({"s": node_index[f.id], "t": node_index[tgt.id], "k": "imports"})
    for s in shown_symbols:
        for tgt, _ in idx.calls_out.get(s.id, ()):
            if tgt.id in keep_syms:
                edges.append({"s": node_index[s.id], "t": node_index[tgt.id], "k": "calls"})
    # Symbols hang off their file implicitly (placement); also expose "defines" for details.
    for s in shown_symbols:
        nodes[node_index[s.id]]["f"] = node_index[s.file_id]

    total = len(files) + len(idx.declared_symbols())
    return {
        "repository": graph.repository.name,
        "fingerprint": graph.source_digest[:16],
        "shown": len(nodes),
        "total": total,
        "nodes": nodes,
        "edges": edges,
    }


def _json_for_script(data: dict) -> str:
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    # Keep the payload inert inside <script>: no tag/comment openers, no JS line terminators.
    return (text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def render_html(graph: CodeGraph, options: Optional[VisualizationOptions] = None) -> str:
    data = build_viz_data(graph, options)
    return _TEMPLATE.replace("__MARKER__", GENERATED_MARKER).replace("__DATA__", _json_for_script(data))


_TEMPLATE = r"""<!DOCTYPE html>
<!-- __MARKER__ -->
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KiteHut graph</title>
<style>
:root{--bg:#0f1419;--panel:#171d24;--fg:#e6e9ee;--muted:#8b95a3;--line:#2a323c;--file:#4da3ff;--class:#f2b84b;--function:#5fd1a0;--method:#c98bf0;--sel:#ff6b6b}
@media (prefers-color-scheme: light){:root{--bg:#f6f7f9;--panel:#fff;--fg:#1a2028;--muted:#5d6876;--line:#d6dbe2;--file:#1f6fd1;--class:#b8791a;--function:#16875f;--method:#8a3fc0;--sel:#d93636}}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}
#app{display:grid;grid-template-columns:260px 1fr 320px;grid-template-rows:auto 1fr;height:100%}
header{grid-column:1/4;padding:8px 14px;border-bottom:1px solid var(--line);background:var(--panel);display:flex;gap:16px;align-items:baseline;flex-wrap:wrap}
header h1{margin:0;font-size:15px}header span{color:var(--muted)}
aside{background:var(--panel);padding:12px;overflow:auto}#left{border-right:1px solid var(--line)}#right{border-left:1px solid var(--line)}
aside h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:14px 0 6px}
label{display:flex;gap:8px;align-items:center;margin:4px 0;cursor:pointer}
input[type=search]{width:100%;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
button{padding:5px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);cursor:pointer}
.dot{width:10px;height:10px;border-radius:50%;display:inline-block}
#stage{position:relative;overflow:hidden}canvas{width:100%;height:100%;display:block;cursor:grab}canvas.drag{cursor:grabbing}
.kv{color:var(--muted)}.path{word-break:break-all;font-family:ui-monospace,monospace;font-size:12px}
ul{padding-left:18px;margin:4px 0}li{margin:2px 0}li a{color:var(--file);cursor:pointer;text-decoration:underline}
#empty{position:absolute;inset:0;display:none;align-items:center;justify-content:center;color:var(--muted)}
@media (max-width:900px){#app{grid-template-columns:1fr;grid-template-rows:auto auto 1fr auto}header{grid-column:1}#left,#right{border:0}}
</style>
</head>
<body>
<div id="app">
<header><h1>KiteHut graph</h1><span id="repo"></span><span id="counts"></span><span id="trunc"></span></header>
<aside id="left">
<input id="q" type="search" placeholder="Search symbols and files" aria-label="Search">
<h2>Show nodes</h2>
<label><input type="checkbox" data-node="file" checked><span class="dot" style="background:var(--file)"></span>Files</label>
<label><input type="checkbox" data-node="class" checked><span class="dot" style="background:var(--class)"></span>Classes</label>
<label><input type="checkbox" data-node="function" checked><span class="dot" style="background:var(--function)"></span>Functions</label>
<label><input type="checkbox" data-node="method" checked><span class="dot" style="background:var(--method)"></span>Methods</label>
<h2>Show relationships</h2>
<label><input type="checkbox" data-edge="imports" checked>Imports (resolved)</label>
<label><input type="checkbox" data-edge="calls" checked>Calls (resolved)</label>
<h2>View</h2><button id="reset" type="button">Reset view</button>
<p class="kv">Drag to pan, scroll to zoom, click a node for details. Only resolved relationships are drawn.</p>
</aside>
<main id="stage"><canvas id="c" aria-label="Repository graph"></canvas><div id="empty">No Python files were discovered in this repository.</div></main>
<aside id="right"><h2>Details</h2><div id="details" class="kv">Select a node.</div></aside>
</div>
<script id="data" type="application/json">__DATA__</script>
<script>
"use strict";
(function(){
var D = JSON.parse(document.getElementById("data").textContent);
var nodes = D.nodes, edges = D.edges;
var out = nodes.map(function(){return [];}), inc = nodes.map(function(){return [];});
edges.forEach(function(e,i){out[e.s].push(i);inc[e.t].push(i);});
var cs = getComputedStyle(document.documentElement);
function col(n){return cs.getPropertyValue("--"+n).trim();}
var cv = document.getElementById("c"), ctx = cv.getContext("2d");
var view = {x:0,y:0,k:1}, sel = -1, hl = null, drag = null;
var showNode = {file:true,"class":true,"function":true,method:true}, showEdge = {imports:true,calls:true};
document.getElementById("repo").textContent = D.repository;
document.getElementById("counts").textContent = D.shown + " of " + D.total + " nodes · fingerprint " + D.fingerprint;
if (D.shown < D.total) document.getElementById("trunc").textContent = "Showing a bounded subset; full data is in graph/repository.json.";
if (!nodes.length) document.getElementById("empty").style.display = "flex";

function resize(){var r=cv.getBoundingClientRect(),d=window.devicePixelRatio||1;cv.width=Math.max(1,r.width*d);cv.height=Math.max(1,r.height*d);ctx.setTransform(d,0,0,d,0,0);draw();}
function fit(){
  if(!nodes.length){view={x:0,y:0,k:1};return;}
  var x0=1e9,y0=1e9,x1=-1e9,y1=-1e9;
  nodes.forEach(function(n){x0=Math.min(x0,n.x);y0=Math.min(y0,n.y);x1=Math.max(x1,n.x);y1=Math.max(y1,n.y);});
  var r=cv.getBoundingClientRect(),w=Math.max(1,x1-x0+120),h=Math.max(1,y1-y0+120);
  var k=Math.min(r.width/w,r.height/h,2.5);
  view={k:k,x:r.width/2-((x0+x1)/2)*k,y:r.height/2-((y0+y1)/2)*k};
}
function visible(i){return showNode[nodes[i].k];}
function sx(n){return n.x*view.k+view.x;} function sy(n){return n.y*view.k+view.y;}
function draw(){
  var r=cv.getBoundingClientRect();ctx.clearRect(0,0,r.width,r.height);
  var neigh=null; if(sel>=0){neigh={};neigh[sel]=1;out[sel].concat(inc[sel]).forEach(function(i){neigh[edges[i].s]=1;neigh[edges[i].t]=1;});}
  edges.forEach(function(e,i){
    if(!showEdge[e.k]||!visible(e.s)||!visible(e.t))return;
    var a=nodes[e.s],b=nodes[e.t],on=sel>=0&&(e.s===sel||e.t===sel);
    ctx.strokeStyle=on?col("sel"):(e.k==="imports"?col("file"):col("function"));
    ctx.globalAlpha=on?0.95:(sel>=0?0.06:0.28);ctx.lineWidth=on?1.6:0.8;
    ctx.beginPath();ctx.moveTo(sx(a),sy(a));ctx.lineTo(sx(b),sy(b));ctx.stroke();
  });
  ctx.globalAlpha=1;
  nodes.forEach(function(n,i){
    if(!visible(i))return;
    var dim=(sel>=0&&!neigh[i])||(hl&&!hl[i]);
    ctx.globalAlpha=dim?0.18:1;ctx.fillStyle=col(n.k);
    var s=sx(n),t=sy(n),rad=Math.max(2,n.r*Math.min(view.k,2.2));
    ctx.beginPath();
    if(n.k==="file"){ctx.rect(s-rad,t-rad,rad*2,rad*2);}else{ctx.arc(s,t,rad,0,6.2832);}
    ctx.fill();
    if(i===sel){ctx.strokeStyle=col("sel");ctx.lineWidth=2;ctx.stroke();}
    if((n.k==="file"&&view.k>0.35)||view.k>1.6||i===sel){ctx.globalAlpha=dim?0.3:1;ctx.fillStyle=col("fg")||"#888";ctx.font="11px system-ui,sans-serif";ctx.fillText(n.n,s+rad+3,t+3);}
  });
  ctx.globalAlpha=1;
}
function pick(px,py){
  var best=-1,bd=1e9;
  nodes.forEach(function(n,i){
    if(!visible(i))return;var dx=sx(n)-px,dy=sy(n)-py,d=dx*dx+dy*dy,rad=Math.max(5,n.r*Math.min(view.k,2.2))+3;
    if(d<=rad*rad&&d<bd){bd=d;best=i;}
  });return best;
}
function li(parent,i,note){var l=document.createElement("li"),a=document.createElement("a");a.textContent=nodes[i].q;a.addEventListener("click",function(){select(i,true);});l.appendChild(a);if(note){l.appendChild(document.createTextNode(" "+note));}parent.appendChild(l);}
function section(box,title,idxs,pick_end,note){
  var h=document.createElement("div");h.className="kv";h.textContent=title+" ("+idxs.length+")";box.appendChild(h);
  var ul=document.createElement("ul");idxs.slice(0,40).forEach(function(i){li(ul,pick_end(edges[i]),note);});box.appendChild(ul);
  if(idxs.length>40){var m=document.createElement("div");m.className="kv";m.textContent="… "+(idxs.length-40)+" more";box.appendChild(m);}
}
function select(i,center){
  sel=i;var box=document.getElementById("details");box.textContent="";
  if(i<0){box.textContent="Select a node.";box.className="kv";draw();return;}
  box.className="";var n=nodes[i];
  var t=document.createElement("div");t.style.fontWeight="600";t.textContent=n.q;box.appendChild(t);
  var k=document.createElement("div");k.className="kv";k.textContent=n.k;box.appendChild(k);
  var p=document.createElement("div");p.className="path";p.textContent=n.p+(n.k==="file"?"":":"+n.l+(n.e!==n.l?"-"+n.e:""));box.appendChild(p);
  var parts=[["Imports",out[i].filter(function(e){return edges[e].k==="imports";}),function(e){return e.t;}],
    ["Imported by",inc[i].filter(function(e){return edges[e].k==="imports";}),function(e){return e.s;}],
    ["Calls",out[i].filter(function(e){return edges[e].k==="calls";}),function(e){return e.t;}],
    ["Called by",inc[i].filter(function(e){return edges[e].k==="calls";}),function(e){return e.s;}]];
  parts.forEach(function(x){if(x[1].length)section(box,x[0],x[1],x[2]);});
  if(center){var r=cv.getBoundingClientRect();view.x=r.width/2-n.x*view.k;view.y=r.height/2-n.y*view.k;}
  draw();
}
cv.addEventListener("mousedown",function(e){drag={x:e.clientX,y:e.clientY,vx:view.x,vy:view.y,moved:false};cv.classList.add("drag");});
window.addEventListener("mousemove",function(e){if(!drag)return;var dx=e.clientX-drag.x,dy=e.clientY-drag.y;if(Math.abs(dx)+Math.abs(dy)>3)drag.moved=true;view.x=drag.vx+dx;view.y=drag.vy+dy;draw();});
window.addEventListener("mouseup",function(e){
  if(!drag)return;var moved=drag.moved;drag=null;cv.classList.remove("drag");
  if(!moved){var r=cv.getBoundingClientRect();select(pick(e.clientX-r.left,e.clientY-r.top),false);}
});
cv.addEventListener("wheel",function(e){
  e.preventDefault();var r=cv.getBoundingClientRect(),px=e.clientX-r.left,py=e.clientY-r.top;
  var f=Math.exp(-e.deltaY*0.0015),nk=Math.min(12,Math.max(0.02,view.k*f)),real=nk/view.k;
  view.x=px-(px-view.x)*real;view.y=py-(py-view.y)*real;view.k=nk;draw();
},{passive:false});
Array.prototype.forEach.call(document.querySelectorAll("[data-node]"),function(el){el.addEventListener("change",function(){showNode[el.dataset.node]=el.checked;draw();});});
Array.prototype.forEach.call(document.querySelectorAll("[data-edge]"),function(el){el.addEventListener("change",function(){showEdge[el.dataset.edge]=el.checked;draw();});});
document.getElementById("reset").addEventListener("click",function(){sel=-1;hl=null;document.getElementById("q").value="";fit();select(-1,false);});
document.getElementById("q").addEventListener("input",function(e){
  var v=e.target.value.trim().toLowerCase();
  if(!v){hl=null;draw();return;}
  hl={};var first=-1;nodes.forEach(function(n,i){if(n.q.toLowerCase().indexOf(v)>=0||n.p.toLowerCase().indexOf(v)>=0){hl[i]=1;if(first<0)first=i;}});
  if(first>=0){var r=cv.getBoundingClientRect();view.x=r.width/2-nodes[first].x*view.k;view.y=r.height/2-nodes[first].y*view.k;}
  draw();
});
window.addEventListener("resize",resize);
resize();fit();draw();
})();
</script>
</body>
</html>
"""

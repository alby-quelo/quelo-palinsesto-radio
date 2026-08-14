# -*- coding: utf-8 -*-
"""Server HTTP headless per Quelo-palinsesto-radio (stile Anti Bianco)."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from db import OverlapError
from listen_stream import open_browser_listen_ffmpeg, pipe_mp3_to, stop_listen_proc
from upload_multipart import parse_multipart

UPLOAD_MAX_BYTES = 512 * 1024 * 1024

if TYPE_CHECKING:
    from engine import PalinsestoEngine

DEFAULT_WEB_PORT = 8890

INDEX_HTML = """<!DOCTYPE html>
<html lang="it"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Quelo Palinsesto Radio</title>
<style>
:root{--bg:#1a1d21;--panel:#242930;--text:#e8eaed;--muted:#9aa0a6;--ok:#3ddc97;--warn:#f5a524;--bad:#ff6b6b;--accent:#5b9fd4}
*{box-sizing:border-box}body{margin:0;padding:1rem;font-family:"IBM Plex Mono",Consolas,monospace;background:#1a1d21;color:var(--text)}
h1{font-size:1.15rem;margin:0}h2{margin:0 0 .7rem;font-size:.9rem;color:var(--accent);text-transform:uppercase;letter-spacing:.05em}
.sub{color:var(--muted);font-size:.8rem}.top{display:flex;justify-content:space-between;gap:1rem;flex-wrap:wrap;margin-bottom:1rem}
.grid{display:grid;gap:1rem;grid-template-columns:1fr}@media(min-width:1100px){.grid{grid-template-columns:1.4fr 1fr}}
section{background:var(--panel);border:1px solid #333a45;border-radius:6px;padding:.9rem 1rem}.full{grid-column:1/-1}
label{display:block;font-size:.78rem;color:var(--muted);margin:.45rem 0 .15rem}
input,select,button,textarea{font:inherit;color:var(--text);background:#15181c;border:1px solid #3a4250;border-radius:4px;padding:.35rem .5rem}
button{cursor:pointer;background:#2f3b4a;border-color:#4a5870;width:auto;margin:.25rem .3rem 0 0}
button.primary{background:#2a5f8f;border-color:#3d7eb5}button.danger{background:#6a2c2c}
.row{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center}.val{font-size:1rem}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.muted{color:var(--muted)}
.vu-row{display:flex;gap:.5rem;align-items:center;flex-wrap:nowrap;margin-top:.5rem}
.vu-track{flex:1 1 auto;min-width:100px;width:0;max-width:95%;height:20px;background:#101318;border:1px solid #333a45;border-radius:4px;overflow:hidden}
.vu-fill{height:100%;width:0%;background:linear-gradient(90deg,#1e9b58,#3ddc97 85%,#f5a524 85%,#ff5a5a 95%);transition:width 40ms linear}
.vu-db{flex:0 0 auto;min-width:5.6rem;white-space:nowrap;text-align:right;font-variant-numeric:tabular-nums}
.tl-wrap{overflow:auto;max-height:480px;border:1px solid #333a45;border-radius:4px;background:#15181c}
.days{display:grid;grid-template-columns:44px repeat(7,minmax(100px,1fr));gap:0;min-width:820px}
.daycol{position:relative;border-left:1px solid #2a313b;min-height:calc(24 * var(--pxh))}
.dayh{position:sticky;top:0;z-index:3;background:#1c2128;border-bottom:1px solid #333a45;padding:.35rem;font-size:.75rem;text-align:center}
.gutter{position:relative;border-right:1px solid #2a313b}
.gutter .hh{position:absolute;left:0;right:0;font-size:.65rem;color:var(--muted);transform:translateY(-0.4em);padding-left:2px}
.hourline{position:absolute;left:0;right:0;border-top:1px solid #2a313b;pointer-events:none}
.nowline{position:absolute;left:0;right:0;height:0;border-top:2px solid #dc5046;z-index:2;pointer-events:none;box-shadow:0 0 4px rgba(220,80,70,.55)}
.clip{position:absolute;left:3px;right:3px;border-radius:3px;padding:2px 4px;font-size:.68rem;overflow:hidden;cursor:pointer;border:1px solid rgba(0,0,0,.35);z-index:1;line-height:1.15}
.clip.playing{outline:2px solid var(--ok)}.clip.sel{outline:2px solid var(--accent)}
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:100;display:flex;align-items:center;justify-content:center;padding:1rem}
.modal{background:var(--panel);border:1px solid #4a5870;border-radius:8px;max-width:520px;width:100%;max-height:90vh;overflow:auto;padding:1rem 1.1rem;box-shadow:0 12px 40px rgba(0,0,0,.45)}
.modal h3{margin:0 0 .75rem;font-size:1rem;color:var(--accent)}
.modal .actions{display:flex;flex-wrap:wrap;gap:.4rem;margin-top:1rem;justify-content:flex-end}
.modal .field{margin:.35rem 0}.modal .field label{display:block;font-size:.75rem;color:var(--muted);margin-bottom:.15rem}
.modal .field input,.modal .field textarea,.modal .field select{width:100%}
.modal .readonly{font-size:.85rem;word-break:break-word;line-height:1.35}
.modal .colorbar{height:8px;border-radius:3px;margin-bottom:.75rem}
.browse{max-height:180px;overflow:auto;background:#101318;border:1px solid #3a4250;border-radius:4px;font-size:.8rem}
.browse div{padding:.25rem .45rem;cursor:pointer;border-bottom:1px solid #222}.browse div:hover{background:#1c2430}
.browse div.picked{background:#2a5f8f}
button.big{padding:.55rem 1rem;font-size:.95rem}
button.step{min-width:2.4rem;font-size:1.15rem;font-weight:700}
input[type=number]::-webkit-inner-spin-button,input[type=number]::-webkit-outer-spin-button{-webkit-appearance:none;margin:0}
input[type=number]{-moz-appearance:textfield;appearance:textfield}
.num-wrap{display:inline-flex;align-items:center;gap:.2rem;vertical-align:middle}
.num-wrap input{width:5.2rem;text-align:center;margin:0}
.num-wrap button{min-width:1.9rem;padding:.28rem .4rem;margin:0;font-size:1.05rem;font-weight:700}
.sil-table{width:100%;margin:.35rem 0 .6rem;font-size:.78rem}
.sil-table th{color:var(--accent);font-weight:600;text-transform:uppercase;letter-spacing:.03em;font-size:.72rem;padding:.35rem .25rem;vertical-align:bottom}
.sil-table td{vertical-align:middle;white-space:nowrap;padding:.3rem .2rem}
.sil-table th.rowlab{color:var(--muted);text-transform:uppercase;text-align:left;width:3.2rem}
.sil-table .unit{color:var(--muted);font-size:.72rem;margin-left:.2rem}
.mix-vu{min-width:140px}
.mix-vu .vu-row{margin:0;gap:.35rem}
.mix-vu .vu-track{height:14px;min-width:70px}
.mix-vu .vu-db{min-width:4.2rem;font-size:.72rem}
.msg{min-height:1.1em;color:var(--warn);font-size:.82rem;margin-top:.4rem}
table{width:100%;border-collapse:collapse;font-size:.8rem}td,th{padding:.25rem .35rem;border-bottom:1px solid #333a45;text-align:left}
a.manual{color:var(--text);text-decoration:none;background:#2f3b4a;border:1px solid #4a5870;border-radius:4px;padding:.4rem .65rem;font-size:.8rem}
.hint{font-size:.75rem;color:var(--muted);margin:.25rem 0 .5rem}
.brand{display:flex;align-items:center;gap:.75rem}
.brand img{height:48px;width:auto;display:block;flex-shrink:0}
.brand h1{margin:0}
</style></head><body>
<div class="top">
  <div class="brand">
    <img src="/docs/logo.png" alt="Quelo" width="48" height="48"/>
    <div><h1>Quelo Palinsesto Radio</h1><p class="sub" id="sub">Web UI</p></div>
  </div>
  <div class="row"><a class="manual" href="/docs/manuale_web.pdf" target="_blank">Manuale PDF</a><span id="clock" class="val"></span></div>
</div>
<div class="grid">
<section>
  <h2>Monitor</h2>
  <div class="row"><span class="muted">Stato</span> <span id="runState" class="val">—</span></div>
  <div class="row"><span class="muted">In onda</span> <span id="onair" class="val">—</span></div>
  <div class="row"><span class="muted">Msg</span> <span id="statusMsg" class="muted">—</span></div>
  <div class="vu-row"><span class="muted">VU</span>
    <div class="vu-track"><div class="vu-fill" id="vuFill"></div></div>
    <span id="vuDb" class="muted vu-db">-∞</span>
  </div>
  <div class="row" style="margin-top:.6rem">
    <button class="primary" id="btnStart">Start</button>
    <button id="btnStop">Stop</button>
    <button id="btnListen">Ascolta da browser</button>
    <label style="margin:0">Volume <input id="vol" type="range" min="0" max="100" value="85" style="width:140px"/></label>
  </div>
  <audio id="listenAudio" preload="none"></audio>
  <p class="hint" id="listenHint">Ascolto remoto: cattura l’uscita audio della macchina (ciò che va agli altoparlanti).</p>
</section>
<section>
  <h2>Settimana</h2>
  <div class="row"><button id="btnPrev">◀</button><strong id="weekLab">—</strong><button id="btnNext">▶</button><button id="btnToday">Oggi</button></div>
  <p class="hint">FILE/PLAYLIST occupano solo la durata reale. LIVE/LINK: 1 ora di default (modificabile).</p>
  <label>Bind<input id="bind" value="0.0.0.0"/></label>
  <label>Porta<input id="port" type="number" value="8890"/></label>
  <div class="row"><button id="btnNet">Applica rete</button></div>
  <div class="msg" id="urls"></div>
</section>
<section class="full">
  <div class="row" style="margin-bottom:.7rem;gap:.4rem">
    <h2 style="margin:0">Timeline</h2>
    <button type="button" id="btnZoomOut" title="Riduci zoom">−</button>
    <span class="muted" id="zoomLab" style="min-width:4.5rem;text-align:center">zoom</span>
    <button type="button" id="btnZoomIn" title="Aumenta zoom">+</button>
  </div>
  <div class="tl-wrap"><div class="days" id="timeline" style="--pxh:100px"></div></div>
</section>
<section>
  <h2>Inserimento Clip</h2>
  <div class="row">
    <button id="btnQueue">In coda</button>
    <button id="btnNow">Da adesso</button>
    <button id="btnManual">Inserimento manuale</button>
  </div>
  <div id="addForm" style="display:none;margin-top:.65rem">
    <label>Tipo<select id="addKind"><option value="file">FILE</option><option value="playlist">PLAYLIST</option><option value="live">LIVE</option><option value="link">LINK</option></select></label>
    <label>Inizio<input id="addStart"/></label>
    <div id="endWrap"><label>Fine (LIVE/LINK)<input id="addEnd"/></label><p class="hint">Default 1 ora dall’inizio; puoi modificarla.</p></div>
    <label>Titolo<input id="addTitle"/></label>
    <label id="pathLab">Path file / playlist<input id="addPath"/></label>
    <div id="devWrap" style="display:none"><label>Device LIVE<input id="addDevice" value="pulse:default"/></label></div>
    <div class="row"><button class="primary" id="btnAdd">Aggiungi clip</button></div>
  </div>
  <div class="row" style="margin-top:.55rem">
    <button class="primary big" id="btnPlCreate">Crea playlist</button>
  </div>
  <div id="plForm" style="display:none;margin-top:.65rem">
    <label>Nome playlist<input id="plName" placeholder="es. Mattina"/></label>
    <label>Cartella di salvataggio<input id="plDir"/></label>
    <div class="row"><button type="button" id="btnPlUseDir">Usa cartella del browser</button></div>
    <p class="hint">Nel Browser file a lato scegli un brano, poi premi + per aggiungerlo ( − toglie l’ultimo).</p>
    <div class="row">
      <button type="button" class="step" id="btnPlAdd">+</button>
      <button type="button" class="step" id="btnPlDel">−</button>
      <button class="primary" type="button" id="btnPlSave">Salva</button>
    </div>
    <div class="browse" id="plTracks" style="margin-top:.45rem;max-height:140px"></div>
    <div class="muted" id="plPick" style="font-size:.75rem;margin-top:.3rem">Nessun file selezionato</div>
    <div class="msg" id="plMsg"></div>
  </div>
  <div class="msg" id="addMsg"></div>
</section>
<section>
  <h2>Browser file</h2>
  <div class="row"><button id="btnHome">Home</button><button id="btnUp">Su</button><button class="primary" id="btnUpload">Upload</button></div>
  <div class="muted" id="browsePath" style="margin:.35rem 0;font-size:.78rem"></div>
  <div class="row" style="margin:.25rem 0 .4rem">
    <input id="mkdirName" placeholder="nome cartella (categoria)" style="flex:1;min-width:8rem"/>
    <button type="button" id="btnMkdir">Crea cartella</button>
  </div>
  <div class="browse" id="browse"></div>
  <div class="msg" id="browseMsg"></div>
</section>
<section>
  <h2>MIXER</h2>
  <div id="mixer"></div>
  <div class="row"><button id="btnMixRefresh">Aggiorna</button></div>
</section>
<section>
  <h2>SETTING</h2>
  <label>ANTI BIANCO playlist
    <div class="row" style="margin-top:.15rem">
      <input id="antiPath" readonly placeholder="clicca per scegliere la playlist…" style="flex:1;min-width:8rem;cursor:pointer"/>
      <button type="button" id="btnAntiPick">Sfoglia</button>
    </div>
  </label>
  <label>Font clip (pt)<input id="fontPt" type="number"/></label>
  <label>Zoom px/ora<input id="zoomPx" type="number" min="30" max="400"/></label>
  <h2 style="margin-top:.8rem">Soglie intervento anti-bianco</h2>
  <table class="sil-table">
    <thead>
      <tr>
        <th></th>
        <th>Innesco</th>
        <th>Rilascio</th>
        <th>Soglia intervento</th>
      </tr>
    </thead>
    <tbody>
      <tr>
        <th class="rowlab">FILE</th>
        <td><input id="sfHold" type="number" step="0.1"/><span class="unit">Sec.</span></td>
        <td><input id="sfRec" type="number" step="0.1"/><span class="unit">Sec.</span></td>
        <td><input id="sfTh" type="number" step="1"/><span class="unit">dB</span></td>
      </tr>
      <tr>
        <th class="rowlab">LINK</th>
        <td><input id="slHold" type="number" step="0.1"/><span class="unit">Sec.</span></td>
        <td><input id="slRec" type="number" step="0.1"/><span class="unit">Sec.</span></td>
        <td><input id="slTh" type="number" step="1"/><span class="unit">dB</span></td>
      </tr>
      <tr>
        <th class="rowlab">LIVE</th>
        <td><input id="svHold" type="number" step="0.1"/><span class="unit">Sec.</span></td>
        <td><input id="svRec" type="number" step="0.1"/><span class="unit">Sec.</span></td>
        <td><input id="svTh" type="number" step="1"/><span class="unit">dB</span></td>
      </tr>
    </tbody>
  </table>
  <div class="row"><button class="primary" id="btnSaveSet">Salva setting</button></div>
  <div class="msg" id="setMsg"></div>
</section>
</div>
<div id="modalRoot" style="display:none"></div>
<script>
const DAY=['Lun','Mar','Mer','Gio','Ven','Sab','Dom'];
let weekMonday=null, clips=[], selectedId=null, browsePath='', pxh=100, playingId=null, lastPickedFile='';
let plTracks=[];
const ZOOM_MIN=30, ZOOM_MAX=400, ZOOM_STEP=6;
function setZoom(v){
  pxh=Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, Math.round(Number(v)||100)));
  const zin=document.getElementById('zoomPx'); if(zin) zin.value=pxh;
  const lab=document.getElementById('zoomLab'); if(lab) lab.textContent=pxh+' px/h';
  renderTimeline();
}
async function api(url, opts){
  const r=await fetch(url, Object.assign({headers:{'Content-Type':'application/json'}}, opts||{}));
  const j=await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(j.error||('HTTP '+r.status));
  return j;
}
function dbFrac(db){ if(db<=-60) return 0; return Math.min(1,(db+60)/60); }
function pad(n){ return String(n).padStart(2,'0'); }
function isoLocal(d){ return d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate())+'T'+pad(d.getHours())+':'+pad(d.getMinutes())+':'+pad(d.getSeconds()); }
function parseISO(s){ return new Date(s); }
function mondayOf(d){ const x=new Date(d); const day=(x.getDay()+6)%7; x.setDate(x.getDate()-day); x.setHours(0,0,0,0); return x; }
function addDays(d,n){ const x=new Date(d); x.setDate(x.getDate()+n); return x; }
function nudgeNum(inp, dir){
  const step=parseFloat(inp.step||'1')||1;
  const min=inp.min===''?null:parseFloat(inp.min);
  const max=inp.max===''?null:parseFloat(inp.max);
  let v=parseFloat(String(inp.value).replace(',','.'));
  if(isNaN(v)) v=(min!=null?min:0);
  v=Math.round((v+dir*step)*10000)/10000;
  if(min!=null && v<min) v=min;
  if(max!=null && v>max) v=max;
  inp.value=String(v);
  inp.dispatchEvent(new Event('input',{bubbles:true}));
  inp.dispatchEvent(new Event('change',{bubbles:true}));
}
function wrapNumberInputs(root){
  (root||document).querySelectorAll('input[type="number"]').forEach(inp=>{
    if(inp.closest('.num-wrap')) return;
    const wrap=document.createElement('span');
    wrap.className='num-wrap';
    const minus=document.createElement('button'); minus.type='button'; minus.textContent='−';
    const plus=document.createElement('button'); plus.type='button'; plus.textContent='+';
    minus.onclick=(e)=>{ e.preventDefault(); nudgeNum(inp,-1); };
    plus.onclick=(e)=>{ e.preventDefault(); nudgeNum(inp,1); };
    inp.parentNode.insertBefore(wrap, inp);
    wrap.appendChild(minus); wrap.appendChild(inp); wrap.appendChild(plus);
  });
}
function syncKindUI(){
  const k=document.getElementById('addKind').value;
  const needEnd=(k==='live'||k==='link');
  document.getElementById('endWrap').style.display=needEnd?'block':'none';
  document.getElementById('devWrap').style.display=k==='live'?'block':'none';
  document.getElementById('pathLab').childNodes[0].textContent=(k==='link'?'URL stream':(k==='live'?'(path ignorato)':'Path file / playlist'));
  if(needEnd){
    const st=parseISO(document.getElementById('addStart').value);
    if(!isNaN(st)){ const en=new Date(st.getTime()+3600000); document.getElementById('addEnd').value=isoLocal(en); }
  }
}
function renderTimeline(){
  const root=document.getElementById('timeline'); root.innerHTML=''; root.style.setProperty('--pxh', pxh+'px');
  if(!weekMonday) return;
  const gut=document.createElement('div'); gut.className='gutter';
  const headG=document.createElement('div'); headG.className='dayh'; headG.textContent=''; gut.appendChild(headG);
  const bodyG=document.createElement('div'); bodyG.style.position='relative'; bodyG.style.height=(24*pxh)+'px';
  for(let h=0;h<=24;h++){
    const line=document.createElement('div'); line.className='hourline'; line.style.top=(h*pxh)+'px'; bodyG.appendChild(line);
    if(h<24){ const lab=document.createElement('div'); lab.className='hh'; lab.style.top=(h*pxh)+'px'; lab.textContent=pad(h)+':00'; bodyG.appendChild(lab); }
  }
  gut.appendChild(bodyG); root.appendChild(gut);
  for(let di=0;di<7;di++){
    const col=document.createElement('div');
    const d=addDays(weekMonday,di);
    const head=document.createElement('div'); head.className='dayh'; head.textContent=DAY[di]+' '+pad(d.getDate())+'/'+pad(d.getMonth()+1); col.appendChild(head);
    const body=document.createElement('div'); body.className='daycol'; body.style.height=(24*pxh)+'px';
    for(let h=0;h<=24;h++){ const line=document.createElement('div'); line.className='hourline'; line.style.top=(h*pxh)+'px'; body.appendChild(line); }
    body.ondblclick=(ev)=>{
      const rect=body.getBoundingClientRect();
      const y=ev.clientY-rect.top; const frac=Math.max(0,Math.min(0.999,y/(24*pxh)));
      const ms=Math.floor(frac*86400000); const dayStart=new Date(d); dayStart.setHours(0,0,0,0);
      const start=new Date(dayStart.getTime()+ms); start.setSeconds(0,0);
      document.getElementById('addStart').value=isoLocal(start);
      const en=new Date(start.getTime()+3600000); document.getElementById('addEnd').value=isoLocal(en);
      syncKindUI();
    };
    // clips for this day
    const day0=new Date(d); day0.setHours(0,0,0,0); const day1=addDays(day0,1);
    clips.forEach(c=>{
      const st=parseISO(c.start_ts), en=parseISO(c.end_ts);
      const a=new Date(Math.max(st, day0)); const b=new Date(Math.min(en, day1));
      if(!(a<b)) return;
      const top=((a-day0)/3600000)*pxh;
      const height=Math.max(6, ((b-a)/3600000)*pxh);
      const el=document.createElement('div');
      el.className='clip'+(selectedId===c.id?' sel':'')+(playingId===c.id?' playing':'');
      el.style.top=top+'px'; el.style.height=height+'px'; el.style.background=c.color||'#3D7AB5';
      const mins=Math.round((b-a)/60000);
      el.textContent=c.show_title+' ('+mins+'m)';
      el.title=c.show_title+' ['+c.kind+'] '+c.start_ts+' → '+c.end_ts+' ('+mins+' min)';
      el.onclick=(e)=>{ e.stopPropagation(); openClipMenu(c); };
      body.appendChild(el);
    });
    body.dataset.day=isoLocal(day0).slice(0,10);
    col.appendChild(body); root.appendChild(col);
  }
  document.getElementById('weekLab').textContent=isoLocal(weekMonday).slice(0,10);
  updateNowLine();
}
function updateNowLine(nowIso){
  if(!weekMonday) return;
  const now=nowIso?parseISO(nowIso):new Date();
  if(isNaN(now)) return;
  const day0=new Date(now); day0.setHours(0,0,0,0);
  const mon=new Date(weekMonday); mon.setHours(0,0,0,0);
  const di=Math.round((day0-mon)/86400000);
  let el=document.getElementById('nowLine');
  if(di<0 || di>6){ if(el) el.remove(); return; }
  const body=document.querySelector('#timeline .daycol[data-day="'+isoLocal(day0).slice(0,10)+'"]');
  if(!body){ if(el) el.remove(); return; }
  if(!el){ el=document.createElement('div'); el.id='nowLine'; el.className='nowline'; el.title='Adesso'; }
  if(el.parentNode!==body) body.appendChild(el);
  const frac=Math.max(0, Math.min(1, (now-day0)/86400000));
  el.style.top=(frac*24*pxh)+'px';
}
async function loadWeek(){
  const q=weekMonday?('?monday='+encodeURIComponent(isoLocal(weekMonday).slice(0,10))):'';
  const data=await api('/api/week'+q);
  weekMonday=parseISO(data.week_monday+'T00:00:00');
  clips=data.clips||[];
  renderTimeline();
}
function selectClip(id){
  selectedId=id;
  renderTimeline();
}
function showAddForm(){
  document.getElementById('addForm').style.display='block';
  syncKindUI();
}
function esc(s){
  return String(s==null?'':s).replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
function closeModal(){
  const root=document.getElementById('modalRoot');
  root.style.display='none'; root.innerHTML='';
}
function openModal(title, bodyHtml, buttons){
  const root=document.getElementById('modalRoot');
  root.style.display='block';
  root.innerHTML=`<div class="modal-bg"><div class="modal" role="dialog"><h3>${esc(title)}</h3><div class="modal-body"></div><div class="actions"></div></div></div>`;
  const bg=root.querySelector('.modal-bg');
  const modal=root.querySelector('.modal');
  const body=root.querySelector('.modal-body');
  const acts=root.querySelector('.actions');
  if(typeof bodyHtml==='string') body.innerHTML=bodyHtml; else body.appendChild(bodyHtml);
  bg.onclick=(e)=>{ if(e.target===bg) closeModal(); };
  modal.onclick=(e)=>e.stopPropagation();
  (buttons||[]).forEach(b=>{
    const btn=document.createElement('button');
    btn.type='button'; btn.textContent=b.label;
    if(b.className) btn.className=b.className;
    btn.onclick=()=>b.onClick();
    acts.appendChild(btn);
  });
}
function kindLabel(k){
  if(k==='live') return 'LIVE (ingresso audio)';
  if(k==='link') return 'LINK (stream HTTP/HTTPS)';
  if(k==='playlist') return 'PLAYLIST (m3u/pls)';
  return 'FILE audio';
}
function clipMins(c){ return Math.max(1, Math.round((parseISO(c.end_ts)-parseISO(c.start_ts))/60000)); }
function openClipMenu(c){
  selectedId=c.id; renderTimeline();
  const html=`<div class="colorbar" style="background:${esc(c.color||'#3D7AB5')}"></div>
    <p class="readonly"><strong>${esc(c.show_title)}</strong><br><span class="muted">${esc(kindLabel(c.kind))} · ${clipMins(c)} min</span><br>${esc(c.start_ts)} → ${esc(c.end_ts)}</p>`;
  openModal('Trasmissione', html, [
    {label:'Elimina', className:'danger', onClick:()=>confirmDeleteClip(c)},
    {label:'Modifica', className:'primary', onClick:()=>openEditClip(c)},
    {label:'Visualizza', onClick:()=>openViewClip(c)},
    {label:'Chiudi', onClick:closeModal},
  ]);
}
async function confirmDeleteClip(c){
  if(!confirm('Eliminare «'+(c.show_title||('#'+c.id))+'»?')) return;
  try{
    await api('/api/clip/'+c.id,{method:'DELETE'});
    if(selectedId===c.id) selectedId=null;
    closeModal();
    await loadWeek();
  }catch(e){ alert(String(e.message||e)); }
}
function openViewClip(c){
  const rows=[
    ['Titolo', c.show_title],
    ['Descrizione', c.description||'—'],
    ['Tipo', kindLabel(c.kind)],
    ['Percorso / URL / device', c.path||'—'],
    ['Inizio', c.start_ts],
    ['Fine', c.end_ts],
    ['Durata', clipMins(c)+' min'],
    ['Colore', c.color||'—'],
    ['Peak gain', (c.peak_gain!=null?Number(c.peak_gain).toFixed(3):'—')],
    ['ID', String(c.id)],
  ];
  let html=`<div class="colorbar" style="background:${esc(c.color||'#3D7AB5')}"></div>`;
  rows.forEach(([lab,val])=>{
    html+=`<div class="field"><label>${esc(lab)}</label><div class="readonly">${esc(val)}</div></div>`;
  });
  openModal('Visualizza', html, [
    {label:'Modifica', className:'primary', onClick:()=>openEditClip(c)},
    {label:'Chiudi', onClick:closeModal},
  ]);
}
function openEditClip(c){
  const pathLab=(c.kind==='link'?'URL stream':(c.kind==='live'?'Device LIVE':'Path file / playlist'));
  const body=document.createElement('div');
  body.innerHTML=`
    <div class="field"><label>Tipo</label><div class="readonly">${esc(kindLabel(c.kind))}</div></div>
    <div class="field"><label>Titolo</label><input id="edTitle" value="${esc(c.title||c.show_title||'')}"/></div>
    <div class="field"><label>Descrizione</label><textarea id="edDesc" rows="3">${esc(c.description||'')}</textarea></div>
    <div class="field"><label>Inizio</label><input id="edStart" value="${esc(c.start_ts)}"/></div>
    <div class="field"><label>Fine</label><input id="edEnd" value="${esc(c.end_ts)}"/></div>
    <div class="field"><label>${esc(pathLab)}</label><input id="edPath" value="${esc(c.path||'')}"/></div>
    <div class="field"><label>Colore (#RRGGBB)</label><input id="edColor" value="${esc(c.color||'#3D7AB5')}"/></div>
    <div class="field"><label>Peak gain</label><input id="edPeak" type="number" step="0.01" value="${esc(c.peak_gain!=null?c.peak_gain:1)}"/></div>`;
  openModal('Modifica', body, [
    {label:'Salva', className:'primary', onClick:async()=>{
      try{
        const payload={
          title:document.getElementById('edTitle').value,
          description:document.getElementById('edDesc').value,
          start_ts:document.getElementById('edStart').value,
          end_ts:document.getElementById('edEnd').value,
          color:document.getElementById('edColor').value,
          peak_gain:+document.getElementById('edPeak').value,
        };
        const path=document.getElementById('edPath').value;
        if(c.kind==='live') payload.device=path; else payload.path=path;
        const updated=await api('/api/clip/'+c.id,{method:'POST',body:JSON.stringify(payload)});
        closeModal();
        await loadWeek();
        openViewClip(updated);
      }catch(e){ alert(String(e.message||e)); }
    }},
    {label:'Annulla', onClick:()=>openClipMenu(c)},
  ]);
  wrapNumberInputs(document.getElementById('modalRoot'));
}
async function setQueueAfterLast(){
  const q=await api('/api/queue-start');
  document.getElementById('addStart').value=q.start_ts;
  document.getElementById('addEnd').value=q.end_ts;
  showAddForm();
  document.getElementById('addMsg').textContent=q.after_clip_id?('In coda dopo clip #'+q.after_clip_id):'Nessuna clip in DB: inizio da adesso';
  document.getElementById('addMsg').className='msg ok';
}
async function setFromNow(){
  const now=new Date(); now.setSeconds(0,0);
  document.getElementById('addStart').value=isoLocal(now);
  document.getElementById('addEnd').value=isoLocal(new Date(now.getTime()+3600000));
  showAddForm();
  document.getElementById('addMsg').textContent='Inizio da adesso';
  document.getElementById('addMsg').className='msg ok';
}
function setManualInsert(){
  document.getElementById('addStart').value='';
  document.getElementById('addEnd').value='';
  showAddForm();
  document.getElementById('addMsg').textContent='Inserimento manuale: digita inizio/fine';
  document.getElementById('addMsg').className='msg';
  document.getElementById('addStart').focus();
}
function isPlaylistFile(p){
  const s=String(p||'').toLowerCase();
  return ['.m3u','.m3u8','.pls'].some(ext=>s.endsWith(ext));
}
async function openAntiPicker(){
  const wrap=document.createElement('div');
  wrap.innerHTML='<p class="hint">Scegli un file .m3u / .m3u8 / .pls (cartelle: clicca per entrare).</p>'
    +'<div class="row"><button type="button" id="pickHome">Home</button><button type="button" id="pickUp">Su</button></div>'
    +'<div class="muted" id="pickPath" style="margin:.35rem 0;font-size:.78rem"></div>'
    +'<div class="browse" id="pickBrowse" style="max-height:280px"></div>';
  openModal('Scegli playlist ANTI BIANCO', wrap, [
    {label:'Nessuna (disattiva)', onClick:()=>{ document.getElementById('antiPath').value=''; closeModal(); }},
    {label:'Chiudi', onClick:closeModal},
  ]);
  let cur='';
  async function go(p){
    const data=await api('/api/browse?path='+encodeURIComponent(p||''));
    cur=data.path;
    document.getElementById('pickPath').textContent=cur;
    const box=document.getElementById('pickBrowse'); box.innerHTML='';
    (data.entries||[]).forEach(e=>{
      const d=document.createElement('div');
      const pl=e.type==='file' && isPlaylistFile(e.path);
      d.textContent=(e.type==='dir'?'📁 ':(pl?'🎵 ':'📄 '))+e.name;
      if(e.type==='file' && !pl) d.style.opacity='0.4';
      d.onclick=()=>{
        if(e.type==='dir') go(e.path);
        else if(pl){ document.getElementById('antiPath').value=e.path; closeModal(); }
      };
      box.appendChild(d);
    });
  }
  document.getElementById('pickHome').onclick=()=>go('');
  document.getElementById('pickUp').onclick=()=>go((cur||'/').replace(/\/+$/,'').split('/').slice(0,-1).join('/')||'/');
  const current=document.getElementById('antiPath').value.trim();
  const start=current?current.replace(/\/[^/]+$/,'') :(browsePath||'');
  await go(start);
}
async function refreshStatus(){
  const st=await api('/api/status');
  document.getElementById('clock').textContent=st.now.replace('T',' ');
  document.getElementById('runState').textContent=st.running?'IN ONDA':'STOP';
  document.getElementById('runState').className='val '+(st.running?'ok':'muted');
  let on='—';
  if(st.anti_bianco) on='ANTI BIANCO';
  else if(st.scheduled) on=st.scheduled.show_title+' ['+st.scheduled.kind+']';
  if(st.failover) on+=' (failover '+st.failover+')';
  document.getElementById('onair').textContent=on;
  document.getElementById('statusMsg').textContent=st.status||'';
  const frac=dbFrac(st.level_db||-120);
  document.getElementById('vuFill').style.width=(frac*100)+'%';
  document.getElementById('vuDb').textContent=(st.level_db<=-90?'-∞':(st.level_db.toFixed(1)+' dB'));
  paintMixerVu(st.mixer_levels);
  if(document.activeElement!==document.getElementById('vol')) document.getElementById('vol').value=Math.round((st.master_volume||0)*100);
  document.getElementById('sub').textContent='DB: '+st.db_path+' · :'+st.port;
  const pid=st.playing_clip_id||null;
  if(pid!==playingId){ playingId=pid; renderTimeline(); }
  updateNowLine(st.now);
}
async function loadSettings(){
  const s=await api('/api/settings');
  document.getElementById('antiPath').value=s.anti_bianco_playlist||'';
  document.getElementById('fontPt').value=s.aspect_clip_font_pt;
  document.getElementById('zoomPx').value=s.aspect_zoom_px_per_hour; setZoom(s.aspect_zoom_px_per_hour);
  document.getElementById('sfHold').value=s.silence.file.hold_sec; document.getElementById('sfRec').value=s.silence.file.recover_sec; document.getElementById('sfTh').value=s.silence.file.thresh_db;
  document.getElementById('slHold').value=s.silence.link.hold_sec; document.getElementById('slRec').value=s.silence.link.recover_sec; document.getElementById('slTh').value=s.silence.link.thresh_db;
  document.getElementById('svHold').value=s.silence.live.hold_sec; document.getElementById('svRec').value=s.silence.live.recover_sec; document.getElementById('svTh').value=s.silence.live.thresh_db;
}
async function loadBrowse(path){
  const data=await api('/api/browse?path='+encodeURIComponent(path||''));
  browsePath=data.path; document.getElementById('browsePath').textContent=browsePath;
  const box=document.getElementById('browse'); box.innerHTML='';
  (data.entries||[]).forEach(e=>{
    const d=document.createElement('div'); d.textContent=(e.type==='dir'?'📁 ':'📄 ')+e.name;
    if(e.type==='file' && e.path===lastPickedFile) d.className='picked';
    d.onclick=()=>{ if(e.type==='dir') loadBrowse(e.path); else {
      lastPickedFile=e.path;
      const addPath=document.getElementById('addPath'); if(addPath) addPath.value=e.path;
      if(['.m3u','.m3u8','.pls'].some(ext=>e.path.toLowerCase().endsWith(ext))) document.getElementById('antiPath').value=e.path;
      const pick=document.getElementById('plPick');
      if(pick) pick.textContent='Selezionato: '+e.path;
      loadBrowse(browsePath);
    }};
    box.appendChild(d);
  });
}
function renderPlTracks(){
  const box=document.getElementById('plTracks'); if(!box) return;
  box.innerHTML='';
  if(!plTracks.length){ const d=document.createElement('div'); d.textContent='(vuota)'; d.style.cursor='default'; box.appendChild(d); return; }
  plTracks.forEach((p,i)=>{
    const d=document.createElement('div');
    d.textContent=(i+1)+'. '+p.split('/').pop();
    d.title=p;
    box.appendChild(d);
  });
}
async function loadMixer(){
  const d=await api('/api/devices'); const box=document.getElementById('mixer'); box.innerHTML='';
  const table=document.createElement('table'); table.innerHTML='<tr><th>Ingresso</th><th>VU</th><th>Vol</th><th>Mute</th></tr>';
  (d.sources||[]).filter(s=>!s.is_monitor).forEach(s=>{
    const tr=document.createElement('tr');
    const active=!s.port || !!s.port_active;
    const vuKey=active?(s.source_name||s.name):'';
    tr.innerHTML=`<td>${esc(s.label)}</td><td class="mix-vu"></td><td></td><td></td>`;
    const vuCell=tr.children[1];
    vuCell.innerHTML=`<div class="vu-row"><div class="vu-track"><div class="vu-fill" data-mix-vu="${esc(vuKey)}"></div></div><span class="muted vu-db" data-mix-db="${esc(vuKey)}">-∞</span></div>`;
    const vol=document.createElement('input'); vol.type='range'; vol.min=0; vol.max=150; vol.value=s.volume_pct;
    vol.onchange=async()=>{ await api('/api/mixer',{method:'POST',body:JSON.stringify({name:s.name,volume_pct:+vol.value})}); };
    const mu=document.createElement('input'); mu.type='checkbox'; mu.checked=!!s.muted;
    mu.onchange=async()=>{ await api('/api/mixer',{method:'POST',body:JSON.stringify({name:s.name,mute:mu.checked})}); };
    tr.children[2].appendChild(vol); tr.children[3].appendChild(mu); table.appendChild(tr);
  });
  box.appendChild(table);
}
function paintMixerVu(levels){
  const map=levels||{};
  document.querySelectorAll('[data-mix-vu]').forEach(el=>{
    const key=el.getAttribute('data-mix-vu')||'';
    const db=key && map[key]!=null ? +map[key] : -120;
    el.style.width=(dbFrac(db)*100)+'%';
  });
  document.querySelectorAll('[data-mix-db]').forEach(el=>{
    const key=el.getAttribute('data-mix-db')||'';
    const db=key && map[key]!=null ? +map[key] : -120;
    el.textContent=(!key||db<=-90)?'-∞':(db.toFixed(1)+' dB');
  });
}
document.getElementById('btnStart').onclick=async()=>{ await api('/api/start',{method:'POST',body:'{}'}); };
document.getElementById('btnStop').onclick=async()=>{ await api('/api/stop',{method:'POST',body:'{}'}); };
let listening=false;
document.getElementById('btnListen').onclick=async()=>{
  const btn=document.getElementById('btnListen');
  const a=document.getElementById('listenAudio');
  const hint=document.getElementById('listenHint');
  if(listening){
    a.pause(); a.removeAttribute('src'); a.load();
    listening=false;
    btn.textContent='Ascolta da browser';
    hint.textContent='Ascolto remoto: cattura l’uscita audio della macchina (ciò che va agli altoparlanti).';
    return;
  }
  a.onerror=()=>{
    listening=false;
    btn.textContent='Ascolta da browser';
    hint.textContent='Ascolto fallito (ffmpeg / Pulse monitor / autoplay bloccato?).';
    a.removeAttribute('src'); a.load();
  };
  a.src='/api/listen?t='+Date.now();
  try{
    await a.play();
    listening=true;
    btn.textContent='Stop ascolto';
    hint.textContent='In ascolto da browser… (ripremi per fermare)';
  }catch(err){
    a.removeAttribute('src'); a.load();
    listening=false;
    btn.textContent='Ascolta da browser';
    hint.textContent='Ascolto non avviato: '+(err&&err.message?err.message:err);
  }
};
document.getElementById('vol').onchange=async()=>{ await api('/api/volume',{method:'POST',body:JSON.stringify({value:(+document.getElementById('vol').value)/100})}); };
document.getElementById('btnPrev').onclick=async()=>{ weekMonday=addDays(weekMonday,-7); await loadWeek(); };
document.getElementById('btnNext').onclick=async()=>{ weekMonday=addDays(weekMonday,7); await loadWeek(); };
document.getElementById('btnToday').onclick=async()=>{ weekMonday=mondayOf(new Date()); await loadWeek(); };
document.getElementById('addKind').onchange=syncKindUI;
document.getElementById('btnQueue').onclick=setQueueAfterLast;
document.getElementById('btnNow').onclick=setFromNow;
document.getElementById('btnManual').onclick=setManualInsert;
document.getElementById('btnPlCreate').onclick=()=>{
  const form=document.getElementById('plForm');
  const open=form.style.display==='none';
  form.style.display=open?'block':'none';
  if(open){
    if(!document.getElementById('plDir').value) document.getElementById('plDir').value=browsePath||'';
    renderPlTracks();
    document.getElementById('plName').focus();
  }
};
document.getElementById('btnPlUseDir').onclick=()=>{ document.getElementById('plDir').value=browsePath||''; };
document.getElementById('btnPlAdd').onclick=()=>{
  const msg=document.getElementById('plMsg');
  if(!lastPickedFile){ msg.textContent='Scegli un file nel Browser a lato, poi +'; msg.className='msg'; return; }
  if(plTracks.includes(lastPickedFile)){ msg.textContent='Già in lista'; msg.className='msg'; return; }
  plTracks.push(lastPickedFile); renderPlTracks();
  msg.textContent='Aggiunto: '+lastPickedFile.split('/').pop(); msg.className='msg ok';
};
document.getElementById('btnPlDel').onclick=()=>{
  const msg=document.getElementById('plMsg');
  if(!plTracks.length){ msg.textContent='Lista già vuota'; msg.className='msg'; return; }
  const gone=plTracks.pop(); renderPlTracks();
  msg.textContent='Rimosso: '+gone.split('/').pop(); msg.className='msg ok';
};
document.getElementById('btnPlSave').onclick=async()=>{
  const msg=document.getElementById('plMsg');
  try{
    const out=await api('/api/playlist/create',{method:'POST',body:JSON.stringify({
      name:document.getElementById('plName').value,
      dir:document.getElementById('plDir').value,
      tracks:plTracks
    })});
    msg.textContent='Salvata: '+out.path+' ('+out.count+' brani)'; msg.className='msg ok';
    const addPath=document.getElementById('addPath'); if(addPath) addPath.value=out.path;
    document.getElementById('addKind').value='playlist';
    showAddForm();
  }catch(e){ msg.textContent=String(e.message||e); msg.className='msg bad'; }
};
document.getElementById('btnAdd').onclick=async()=>{
  const kind=document.getElementById('addKind').value; const msg=document.getElementById('addMsg');
  try{
    const body={title:document.getElementById('addTitle').value, start_ts:document.getElementById('addStart').value, end_ts:document.getElementById('addEnd').value, path:document.getElementById('addPath').value, url:document.getElementById('addPath').value, device:document.getElementById('addDevice').value};
    const created=await api('/api/clip/'+kind,{method:'POST',body:JSON.stringify(body)});
    msg.textContent='OK: '+created.show_title+' ('+Math.round(created.duration_ms/60000)+' min) '+created.start_ts+' → '+created.end_ts;
    msg.className='msg ok';
    // prepara coda automatica al termine del clip appena inserito
    document.getElementById('addStart').value=created.end_ts;
    const en=new Date(parseISO(created.end_ts).getTime()+3600000); document.getElementById('addEnd').value=isoLocal(en);
    showAddForm();
    await loadWeek(); selectClip(created.id);
  }catch(e){ msg.textContent=String(e.message||e); msg.className='msg bad'; }
};
document.getElementById('antiPath').onclick=openAntiPicker;
document.getElementById('btnAntiPick').onclick=openAntiPicker;
document.getElementById('btnSaveSet').onclick=async()=>{
  try{
    await api('/api/settings',{method:'POST',body:JSON.stringify({
      anti_bianco_playlist:document.getElementById('antiPath').value,
      aspect_clip_font_pt:+document.getElementById('fontPt').value,
      aspect_zoom_px_per_hour:+document.getElementById('zoomPx').value,
      silence:{
        file:{hold_sec:+document.getElementById('sfHold').value, recover_sec:+document.getElementById('sfRec').value, thresh_db:+document.getElementById('sfTh').value},
        link:{hold_sec:+document.getElementById('slHold').value, recover_sec:+document.getElementById('slRec').value, thresh_db:+document.getElementById('slTh').value},
        live:{hold_sec:+document.getElementById('svHold').value, recover_sec:+document.getElementById('svRec').value, thresh_db:+document.getElementById('svTh').value}
      }
    })});
    setZoom(document.getElementById('zoomPx').value);
    document.getElementById('setMsg').textContent='Salvato'; document.getElementById('setMsg').className='msg ok';
  }catch(e){ document.getElementById('setMsg').textContent=String(e.message||e); document.getElementById('setMsg').className='msg bad'; }
};
document.getElementById('btnZoomOut').onclick=()=>setZoom(pxh-ZOOM_STEP);
document.getElementById('btnZoomIn').onclick=()=>setZoom(pxh+ZOOM_STEP);
document.getElementById('btnHome').onclick=()=>loadBrowse('');
document.getElementById('btnUp').onclick=()=>loadBrowse(browsePath.split('/').slice(0,-1).join('/')||'/');
document.getElementById('btnUpload').onclick=()=>openUploadChoice();
const UP_AUDIO_EXT=['.wav','.mp3','.flac','.ogg','.opus','.m4a','.aac','.wma','.aiff','.aif'];
function isAudioFileName(n){ const s=String(n||'').toLowerCase(); return UP_AUDIO_EXT.some(ext=>s.endsWith(ext)); }
async function uploadApi(fd){
  const r=await fetch('/api/upload',{method:'POST',body:fd});
  const j=await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(j.error||('HTTP '+r.status));
  return j;
}
function openUploadChoice(){
  const wrap=document.createElement('div');
  wrap.innerHTML='<p class="hint">Cosa vuoi caricare sulla macchina?</p>'
    +'<p class="hint">Per creare una playlist dai file già sul server usa «Crea playlist» in Inserimento Clip.</p>';
  openModal('Upload', wrap, [
    {label:'File audio', className:'primary', onClick:()=>openUploadFile()},
    {label:'Playlist già pronta', className:'primary', onClick:()=>openUploadPlaylist()},
    {label:'Chiudi', onClick:closeModal},
  ]);
}
let upAudioFile=null, upPlFile=null, upAudioList=[];
function uploadDestBlock(destVal){
  return '<label>Cartella destinazione'
    +'<div class="row" style="margin-top:.15rem">'
    +'<input id="upDest" readonly placeholder="clicca per scegliere la cartella…" value="'+esc(destVal||browsePath||'')+'" style="flex:1;min-width:8rem;cursor:pointer"/>'
    +'<button type="button" id="upPickDir">Sfoglia</button>'
    +'</div></label>'
    +'<div class="row"><button type="button" id="upUseBrowse">Usa cartella proposta</button></div>'
    +'<div class="row" style="margin-top:.35rem"><input id="upMkdir" placeholder="nuova cartella (opzionale)" style="flex:1;min-width:8rem"/><button type="button" id="upMkdirBtn">Crea cartella</button></div>'
    +'<div class="msg" id="upMsg"></div>';
}
async function openDirPicker(onPick, startPath){
  const wrap=document.createElement('div');
  wrap.innerHTML='<p class="hint">Naviga e premi «Seleziona questa cartella».</p>'
    +'<div class="row"><button type="button" id="dirHome">Home</button><button type="button" id="dirUp">Su</button></div>'
    +'<div class="muted" id="dirPath" style="margin:.35rem 0;font-size:.78rem"></div>'
    +'<div class="browse" id="dirBrowse" style="max-height:280px"></div>';
  let cur='';
  async function go(p){
    const data=await api('/api/browse?path='+encodeURIComponent(p||''));
    cur=data.path;
    document.getElementById('dirPath').textContent=cur;
    const box=document.getElementById('dirBrowse'); box.innerHTML='';
    (data.entries||[]).filter(e=>e.type==='dir').forEach(e=>{
      const d=document.createElement('div'); d.textContent='📁 '+e.name;
      d.onclick=()=>go(e.path);
      box.appendChild(d);
    });
  }
  openModal('Scegli cartella', wrap, [
    {label:'Seleziona questa cartella', className:'primary', onClick:()=>{ const p=cur; onPick(p); }},
    {label:'Annulla', onClick:()=>onPick(null)},
  ]);
  document.getElementById('dirHome').onclick=()=>go('');
  document.getElementById('dirUp').onclick=()=>go((cur||'/').replace(/\/+$/,'').split('/').slice(0,-1).join('/')||'/');
  await go(startPath||browsePath||'');
}
function wireUploadDest(mode){
  const pick=async()=>{
    const start=document.getElementById('upDest').value.trim()||browsePath||'';
    await openDirPicker((p)=>{
      const dest=(p==null?start:p);
      if(mode==='file') openUploadFile(dest);
      else openUploadPlaylist(dest);
    }, start);
  };
  document.getElementById('upDest').onclick=pick;
  document.getElementById('upPickDir').onclick=pick;
  document.getElementById('upUseBrowse').onclick=()=>{ document.getElementById('upDest').value=browsePath||''; };
  document.getElementById('upMkdirBtn').onclick=async()=>{
    const msg=document.getElementById('upMsg');
    try{
      const parent=document.getElementById('upDest').value.trim()||browsePath;
      const out=await api('/api/mkdir',{method:'POST',body:JSON.stringify({path:parent,name:document.getElementById('upMkdir').value})});
      document.getElementById('upDest').value=out.path;
      document.getElementById('upMkdir').value='';
      msg.textContent='Cartella creata: '+out.path; msg.className='msg ok';
      await loadBrowse(out.path);
    }catch(e){ msg.textContent=String(e.message||e); msg.className='msg bad'; }
  };
}
function openUploadFile(presetDest){
  const wrap=document.createElement('div');
  wrap.innerHTML=uploadDestBlock(presetDest)
    +'<label style="margin-top:.6rem">File audio<input id="upFile" type="file" accept="'+UP_AUDIO_EXT.join(',')+',audio/*"/></label>'
    +'<p class="hint" id="upFileLab">'+(upAudioFile?('Selezionato: '+upAudioFile.name):'Nessun file selezionato')+'</p>';
  openModal('Upload file audio', wrap, [
    {label:'Carica', className:'primary', onClick:async()=>{
      const msg=document.getElementById('upMsg');
      try{
        const f=upAudioFile;
        if(!f) throw new Error('Scegli un file audio');
        if(!isAudioFileName(f.name)) throw new Error('Formato non supportato: '+f.name);
        const fd=new FormData();
        fd.append('mode','file');
        fd.append('dest',document.getElementById('upDest').value.trim());
        fd.append('files',f,f.name);
        msg.textContent='Caricamento…'; msg.className='msg';
        const out=await uploadApi(fd);
        msg.textContent='OK: '+out.path; msg.className='msg ok';
        upAudioFile=null;
        await loadBrowse(out.dir||document.getElementById('upDest').value.trim());
        const addPath=document.getElementById('addPath'); if(addPath) addPath.value=out.path;
      }catch(e){ msg.textContent=String(e.message||e); msg.className='msg bad'; }
    }},
    {label:'Indietro', onClick:()=>{ upAudioFile=null; openUploadChoice(); }},
    {label:'Chiudi', onClick:()=>{ upAudioFile=null; closeModal(); }},
  ]);
  wireUploadDest('file');
  document.getElementById('upFile').onchange=()=>{
    const f=document.getElementById('upFile').files&&document.getElementById('upFile').files[0];
    upAudioFile=f||null;
    document.getElementById('upFileLab').textContent=upAudioFile?('Selezionato: '+upAudioFile.name):'Nessun file selezionato';
  };
}
function openUploadPlaylist(presetDest){
  const wrap=document.createElement('div');
  wrap.innerHTML=uploadDestBlock(presetDest)
    +'<label style="margin-top:.6rem">File playlist (.m3u / .m3u8 / .pls)<input id="upPlFile" type="file" accept=".m3u,.m3u8,.pls"/></label>'
    +'<p class="hint" id="upPlLab">'+(upPlFile?('Playlist: '+upPlFile.name):'Nessuna playlist selezionata')+'</p>'
    +'<p class="hint">Poi carica i file audio (o la cartella) a cui la playlist fa riferimento.</p>'
    +'<div class="row">'
    +'<button type="button" class="step" id="upPlDel">−</button>'
    +'<button type="button" id="upPlPickFiles">Scegli file audio…</button>'
    +'<button type="button" id="upPlPickDir">Scegli cartella audio…</button>'
    +'</div>'
    +'<input id="upPlFiles" type="file" multiple accept="'+UP_AUDIO_EXT.join(',')+',audio/*" style="display:none"/>'
    +'<input id="upPlDir" type="file" webkitdirectory multiple style="display:none"/>'
    +'<div class="browse" id="upPlList" style="margin-top:.45rem;max-height:160px"></div>';
  function renderList(){
    const box=document.getElementById('upPlList'); box.innerHTML='';
    if(!upAudioList.length){ const d=document.createElement('div'); d.textContent='(nessun audio)'; d.style.cursor='default'; box.appendChild(d); return; }
    upAudioList.forEach((f,i)=>{ const d=document.createElement('div'); d.textContent=(i+1)+'. '+f.name; d.title=f.name; box.appendChild(d); });
  }
  function addFileList(list){
    const msg=document.getElementById('upMsg');
    let n=0;
    Array.from(list||[]).forEach(f=>{
      if(!isAudioFileName(f.name)) return;
      if(upAudioList.some(x=>x.name===f.name && x.size===f.size)) return;
      upAudioList.push(f); n++;
    });
    renderList();
    msg.textContent=n?('Aggiunti '+n+' file audio'):'Nessun file audio nuovo';
    msg.className=n?'msg ok':'msg';
  }
  openModal('Upload playlist già pronta', wrap, [
    {label:'Carica', className:'primary', onClick:async()=>{
      const msg=document.getElementById('upMsg');
      try{
        if(!upPlFile) throw new Error('Seleziona il file playlist');
        if(!upAudioList.length) throw new Error('Carica i file audio referenziati');
        const fd=new FormData();
        fd.append('mode','playlist');
        fd.append('dest',document.getElementById('upDest').value.trim());
        fd.append('playlist',upPlFile,upPlFile.name);
        upAudioList.forEach(f=>fd.append('files',f,f.name));
        msg.textContent='Caricamento…'; msg.className='msg';
        const out=await uploadApi(fd);
        msg.textContent='OK: '+(out.playlist||'')+' + '+out.count+' brani'; msg.className='msg ok';
        upPlFile=null; upAudioList=[];
        await loadBrowse(out.dir||document.getElementById('upDest').value.trim());
        const addPath=document.getElementById('addPath'); if(addPath) addPath.value=out.playlist||'';
        document.getElementById('addKind').value='playlist';
      }catch(e){ msg.textContent=String(e.message||e); msg.className='msg bad'; }
    }},
    {label:'Indietro', onClick:()=>{ upPlFile=null; upAudioList=[]; openUploadChoice(); }},
    {label:'Chiudi', onClick:()=>{ upPlFile=null; upAudioList=[]; closeModal(); }},
  ]);
  wireUploadDest('playlist');
  renderList();
  document.getElementById('upPlFile').onchange=()=>{
    const f=document.getElementById('upPlFile').files&&document.getElementById('upPlFile').files[0];
    upPlFile=f||null;
    document.getElementById('upPlLab').textContent=upPlFile?('Playlist: '+upPlFile.name):'Nessuna playlist selezionata';
  };
  document.getElementById('upPlPickFiles').onclick=()=>document.getElementById('upPlFiles').click();
  document.getElementById('upPlPickDir').onclick=()=>document.getElementById('upPlDir').click();
  document.getElementById('upPlFiles').onchange=()=>{ addFileList(document.getElementById('upPlFiles').files); document.getElementById('upPlFiles').value=''; };
  document.getElementById('upPlDir').onchange=()=>{ addFileList(document.getElementById('upPlDir').files); document.getElementById('upPlDir').value=''; };
  document.getElementById('upPlDel').onclick=()=>{
    const msg=document.getElementById('upMsg');
    if(!upAudioList.length){ msg.textContent='Lista già vuota'; msg.className='msg'; return; }
    const gone=upAudioList.pop(); renderList();
    msg.textContent='Rimosso: '+gone.name; msg.className='msg ok';
  };
}
document.getElementById('btnMkdir').onclick=async()=>{
  const msg=document.getElementById('browseMsg');
  const name=(document.getElementById('mkdirName').value||'').trim();
  try{
    const out=await api('/api/mkdir',{method:'POST',body:JSON.stringify({path:browsePath,name})});
    document.getElementById('mkdirName').value='';
    msg.textContent='Cartella creata: '+out.name; msg.className='msg ok';
    const plDir=document.getElementById('plDir');
    if(plDir && document.getElementById('plForm').style.display!=='none') plDir.value=out.path;
    await loadBrowse(out.path);
  }catch(e){ msg.textContent=String(e.message||e); msg.className='msg bad'; }
};
document.getElementById('btnMixRefresh').onclick=loadMixer;
document.getElementById('btnNet').onclick=async()=>{
  const bind=document.getElementById('bind').value||'0.0.0.0'; const port=+document.getElementById('port').value||8890;
  const meta=await api('/api/network',{method:'POST',body:JSON.stringify({bind,port})});
  document.getElementById('urls').textContent=(meta.urls||[]).join(' · ');
  alert('Server riavviato. Ricarica la pagina se la porta è cambiata.');
};
(async()=>{
  wrapNumberInputs(document);
  weekMonday=mondayOf(new Date());
  syncKindUI();
  await loadWeek(); await loadSettings(); await loadBrowse(''); await loadMixer();
  const meta=await api('/api/meta'); document.getElementById('urls').textContent=(meta.urls||[]).join(' · ');
  document.getElementById('bind').value=meta.bind; document.getElementById('port').value=meta.port;
  setInterval(async()=>{ try{ await refreshStatus(); }catch(e){} }, 150);
  setInterval(async()=>{ try{ await loadWeek(); }catch(e){} }, 20000);
})();
</script></body></html>"""


def local_ips() -> list[str]:
    import socket

    ips: list[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip not in ips:
            ips.insert(0, ip)
    except OSError:
        pass
    return ips


def web_urls(bind: str, port: int) -> list[str]:
    port = int(port)
    bind = (bind or "0.0.0.0").strip() or "0.0.0.0"
    urls: list[str] = []
    if bind in ("0.0.0.0", "::"):
        for ip in local_ips() or ["127.0.0.1"]:
            urls.append(f"http://{ip}:{port}/")
        urls.append(f"http://127.0.0.1:{port}/")
    else:
        urls.append(f"http://{bind}:{port}/")
        if bind != "127.0.0.1":
            urls.append(f"http://127.0.0.1:{port}/")
    out: list[str] = []
    seen: set[str] = set()
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _json_bytes(obj: object, code: int = 200) -> tuple[int, bytes, str]:
    return code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8"


def _docs_dir() -> Path:
    # .../share/quelo-palinsesto-radio/web.py -> ../../docs
    return Path(__file__).resolve().parents[2] / "docs"


def make_handler(engine: "PalinsestoEngine", holder: dict):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            return

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length > 0 else b"{}"
            data = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(data, dict):
                raise ValueError("JSON oggetto richiesto")
            return data

        def _err(self, exc: Exception, code: int = 400) -> None:
            c, body, ct = _json_bytes({"error": str(exc)}, code)
            self._send(c, body, ct)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)
            try:
                if path in ("/", "/index.html"):
                    self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
                    return
                if path.startswith("/docs/"):
                    name = Path(path).name
                    doc = _docs_dir() / name
                    if not doc.is_file():
                        self._err(FileNotFoundError(name), 404)
                        return
                    data = doc.read_bytes()
                    ctype = "application/pdf" if name.endswith(".pdf") else "application/octet-stream"
                    if name.endswith(".png"):
                        ctype = "image/png"
                    self._send(200, data, ctype)
                    return
                if path == "/api/status":
                    c, b, ct = _json_bytes(engine.status())
                    self._send(c, b, ct)
                    return
                if path == "/api/meta":
                    bind, port = engine.web_listen()
                    c, b, ct = _json_bytes(
                        {"port": port, "bind": bind, "urls": web_urls(bind, port), "home": str(Path.home())}
                    )
                    self._send(c, b, ct)
                    return
                if path == "/api/week":
                    monday = (qs.get("monday") or [None])[0]
                    c, b, ct = _json_bytes(engine.list_week(monday))
                    self._send(c, b, ct)
                    return
                if path == "/api/queue-start":
                    day = (qs.get("day") or [None])[0]
                    c, b, ct = _json_bytes(engine.queue_start(day))
                    self._send(c, b, ct)
                    return
                if path == "/api/settings":
                    c, b, ct = _json_bytes(engine.get_settings())
                    self._send(c, b, ct)
                    return
                if path == "/api/devices":
                    c, b, ct = _json_bytes(engine.devices())
                    self._send(c, b, ct)
                    return
                if path == "/api/browse":
                    p = (qs.get("path") or [""])[0]
                    c, b, ct = _json_bytes(engine.browse(p))
                    self._send(c, b, ct)
                    return
                if path == "/api/listen":
                    self._stream_browser_listen()
                    return
                if path.startswith("/api/clip/"):
                    cid = int(path.rsplit("/", 1)[-1])
                    c, b, ct = _json_bytes(engine.get_clip(cid))
                    self._send(c, b, ct)
                    return
                self._err(ValueError("not found"), 404)
            except Exception as exc:  # noqa: BLE001
                self._err(exc, 400)

        def _stream_browser_listen(self) -> None:
            try:
                proc = open_browser_listen_ffmpeg()
            except Exception as exc:  # noqa: BLE001
                self._err(exc, 503)
                return
            # Se ffmpeg muore subito, restituisci stderr invece di uno stream vuoto
            time.sleep(0.15)
            if proc.poll() is not None:
                err = b""
                if proc.stderr is not None:
                    try:
                        err = proc.stderr.read() or b""
                    except Exception:
                        err = b""
                msg = err.decode("utf-8", errors="replace").strip() or "ffmpeg terminato subito"
                stop_listen_proc(proc)
                self._err(RuntimeError(msg), 503)
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Cache-Control", "no-store, no-cache")
            self.send_header("Pragma", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            pipe_mp3_to(self.wfile, proc)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                ctype = self.headers.get("Content-Type") or ""
                if path == "/api/upload" and "multipart/form-data" in ctype:
                    self._handle_upload()
                    return
                data = self._read_json()
                if path == "/api/start":
                    c, b, ct = _json_bytes(engine.start())
                    self._send(c, b, ct)
                    return
                if path == "/api/stop":
                    c, b, ct = _json_bytes(engine.stop())
                    self._send(c, b, ct)
                    return
                if path == "/api/volume":
                    c, b, ct = _json_bytes(engine.set_volume(float(data.get("value", 0.85))))
                    self._send(c, b, ct)
                    return
                if path == "/api/settings":
                    c, b, ct = _json_bytes(engine.set_settings(data))
                    self._send(c, b, ct)
                    return
                if path == "/api/mixer":
                    c, b, ct = _json_bytes(engine.mixer_set(data))
                    self._send(c, b, ct)
                    return
                if path == "/api/network":
                    bind = str(data.get("bind") or "0.0.0.0")
                    port = int(data.get("port") or DEFAULT_WEB_PORT)
                    engine.set_web_listen(bind, port)
                    # restart server
                    old = holder.get("server")
                    new = start_web_server(engine, host=bind, port=port, attach=True, holder=holder)
                    stop_web_server(old)
                    holder["server"] = new
                    c, b, ct = _json_bytes({"bind": bind, "port": port, "urls": web_urls(bind, port)})
                    self._send(c, b, ct)
                    return
                if path == "/api/clip/file":
                    c, b, ct = _json_bytes(engine.add_file_clip(data))
                    self._send(c, b, ct)
                    return
                if path == "/api/clip/playlist":
                    c, b, ct = _json_bytes(engine.add_playlist_clip(data))
                    self._send(c, b, ct)
                    return
                if path == "/api/playlist/create":
                    c, b, ct = _json_bytes(engine.create_playlist_file(data))
                    self._send(c, b, ct)
                    return
                if path == "/api/mkdir":
                    c, b, ct = _json_bytes(engine.mkdir(data))
                    self._send(c, b, ct)
                    return
                if path == "/api/clip/live":
                    c, b, ct = _json_bytes(engine.add_live_clip(data))
                    self._send(c, b, ct)
                    return
                if path == "/api/clip/link":
                    c, b, ct = _json_bytes(engine.add_link_clip(data))
                    self._send(c, b, ct)
                    return
                if path.startswith("/api/clip/") and path.count("/") == 3:
                    # /api/clip/<id> update
                    cid = int(path.rsplit("/", 1)[-1])
                    c, b, ct = _json_bytes(engine.update_clip(cid, data))
                    self._send(c, b, ct)
                    return
                self._err(ValueError("not found"), 404)
            except OverlapError as exc:
                self._err(exc, 409)
            except Exception as exc:  # noqa: BLE001
                self._err(exc, 400)

        def _handle_upload(self) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0:
                raise ValueError("upload vuoto")
            if length > UPLOAD_MAX_BYTES:
                raise ValueError(f"Upload troppo grande (max {UPLOAD_MAX_BYTES // (1024*1024)} MB)")
            body = self.rfile.read(length)
            fields, files = parse_multipart(body, self.headers.get("Content-Type") or "")
            mode = (fields.get("mode") or "file").strip().lower()
            dest = (fields.get("dest") or "").strip()
            playlist_file = None
            audio: list[tuple[str, bytes]] = []
            for field, fn, data in files:
                if not fn:
                    continue
                if field == "playlist":
                    playlist_file = (fn, data)
                else:
                    audio.append((fn, data))
            result = engine.upload_media(
                dest_dir=dest,
                mode=mode,
                files=audio,
                playlist_file=playlist_file,
            )
            c, b, ct = _json_bytes(result)
            self._send(c, b, ct)

        def do_DELETE(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                if path.startswith("/api/clip/"):
                    cid = int(path.rsplit("/", 1)[-1])
                    c, b, ct = _json_bytes(engine.delete_clip(cid))
                    self._send(c, b, ct)
                    return
                self._err(ValueError("not found"), 404)
            except Exception as exc:  # noqa: BLE001
                self._err(exc, 400)

    return Handler


def stop_web_server(server: ThreadingHTTPServer | None) -> None:
    if server is None:
        return
    try:
        server.shutdown()
    except Exception:
        pass
    try:
        server.server_close()
    except Exception:
        pass


def start_web_server(
    engine: "PalinsestoEngine",
    *,
    host: str = "0.0.0.0",
    port: int = DEFAULT_WEB_PORT,
    attach: bool = True,
    holder: dict | None = None,
) -> ThreadingHTTPServer:
    holder = holder if holder is not None else {}
    engine.set_web_listen(host, port)
    handler = make_handler(engine, holder)
    server = ThreadingHTTPServer((host, int(port)), handler)
    holder["server"] = server
    if attach:
        t = threading.Thread(target=server.serve_forever, name="palinsesto-web", daemon=True)
        t.start()
        holder["thread"] = t
    return server

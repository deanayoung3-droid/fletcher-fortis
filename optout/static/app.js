// All page behaviour lives here (CSP: script-src 'self', no inline handlers).
function theme(){const d=document.documentElement.classList.toggle('dark');try{localStorage.theme=d?'dark':'light'}catch(e){}}
function sb(){document.getElementById('sb')?.classList.toggle('-translate-x-full');document.getElementById('scrim')?.classList.toggle('hidden')}
document.addEventListener('click',e=>{
  const a=e.target.closest('[data-action]');if(a){e.preventDefault();({theme,sb})[a.dataset.action]?.();return}
  const row=e.target.closest('[data-href]');if(row&&!e.target.closest('a,button,form')){location=row.dataset.href;return}
  const add=e.target.closest('[data-add-row]');if(add){e.preventDefault();addRow(add.dataset.addRow);return}
  const del=e.target.closest('[data-del-row]');if(del){e.preventDefault();const list=del.closest('[data-list]');del.closest('[data-row]').remove();renumber(list);return}
  const cp=e.target.closest('[data-copy]');if(cp){e.preventDefault();copyText(cp);return}
  const cf=e.target.closest('[data-confirm]');if(cf&&!confirm(cf.dataset.confirm)){e.preventDefault()}
});
document.addEventListener('change',e=>{const f=e.target.closest('[data-filename]');if(f){document.getElementById(f.dataset.filename).textContent=f.files[0]?.name||'Choose file'}});
addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key==='k'){e.preventDefault();document.getElementById('k')?.focus()}});
document.getElementById('k')?.addEventListener('input',e=>{const v=e.target.value.toLowerCase();document.querySelectorAll('[data-q]').forEach(n=>n.style.display=n.dataset.q.toLowerCase().includes(v)?'':'none')});
// profile editor: repeatable rows from <template id="tpl-NAME">
function addRow(name){const list=document.querySelector(`[data-list="${name}"]`),tpl=document.getElementById('tpl-'+name);if(!list||!tpl)return;
  const max=+list.dataset.max||20;if(list.querySelectorAll('[data-row]').length>=max)return;
  list.appendChild(tpl.content.cloneNode(true));renumber(list);list.querySelector('[data-row]:last-child input:not([type=radio])')?.focus()}
function renumber(list){if(!list)return;list.querySelectorAll('[data-row]').forEach((r,i)=>r.querySelectorAll('input[type=radio][name=addr_current]').forEach(x=>x.value=i));
  const n=list.querySelectorAll('[data-row]').length,empty=list.parentElement.querySelector('[data-empty]');if(empty)empty.hidden=n>0}
document.querySelectorAll('[data-list]').forEach(renumber);
document.querySelectorAll('main .tile, main .reveal-me').forEach((n,i)=>{n.classList.add('reveal');n.style.setProperty('--i',Math.min(i,12))});
if(!matchMedia('(prefers-reduced-motion: reduce)').matches)document.querySelectorAll('[data-count]').forEach(n=>{const to=+n.dataset.count,t0=performance.now(),d=900;if(!to)return;
 const f=t=>{const p=Math.min(1,(t-t0)/d),e=1-Math.pow(1-p,3);n.textContent=Math.round(to*e);if(p<1)requestAnimationFrame(f)};n.textContent='0';requestAnimationFrame(f)});
// clipboard API only exists on https/localhost; on a plain-http LAN address fall back to selecting the field + execCommand
function copyText(btn){const el=document.getElementById(btn.dataset.copy);if(!el)return;const ok=()=>{const s=btn.querySelector('span');if(s){s.textContent='Copied';setTimeout(()=>s.textContent='Copy',2000)}};
 const legacy=()=>{el.focus();el.select();try{document.execCommand('copy');ok()}catch(e){}};
 if(navigator.clipboard&&window.isSecureContext)navigator.clipboard.writeText(el.value).then(ok,legacy);else legacy()}
// US phone inputs: format as (555) 555-0123 while typing; the server stores the 10 digits only
function fmtPhone(v){let d=v.replace(/\D/g,'');if(d.length===11&&d[0]==='1')d=d.slice(1);d=d.slice(0,10);
 if(d.length<4)return d.length?'('+d:'';if(d.length<7)return'('+d.slice(0,3)+') '+d.slice(3);return'('+d.slice(0,3)+') '+d.slice(3,6)+'-'+d.slice(6)}
document.addEventListener('input',e=>{const t=e.target;if(!t.matches?.('input[type=tel],input[name=phone]'))return;
 if(e.inputType&&e.inputType.startsWith('delete')&&!/\d$/.test(t.value))return;const end=t.selectionStart===t.value.length;t.value=fmtPhone(t.value);if(end)t.setSelectionRange(t.value.length,t.value.length)});
document.querySelectorAll('input[type=tel],input[name=phone]').forEach(t=>{if(t.value)t.value=fmtPhone(t.value)});

/* Signature pad: touch, pen and mouse via Pointer Events. Stores normalised strokes [[[x,y],..],..] in #sig. */
(()=>{const cv=document.getElementById('sigpad');if(!cv)return;const form=cv.closest('form'),out=document.getElementById('sig'),hint=form.querySelector('[data-sighint]');
 let strokes=[],cur=null,ctx;const dark=()=>document.documentElement.classList.contains('dark');
 function size(){const r=cv.getBoundingClientRect(),d=window.devicePixelRatio||1;cv.width=r.width*d;cv.height=r.height*d;ctx=cv.getContext('2d');ctx.scale(d,d);ctx.lineCap='round';ctx.lineJoin='round';ctx.lineWidth=2.2;redraw()}
 function redraw(){const r=cv.getBoundingClientRect();ctx.clearRect(0,0,r.width,r.height);ctx.strokeStyle=dark()?'#f5f5f4':'#1e1b4b';
  for(const s of strokes){ctx.beginPath();s.forEach(([x,y],i)=>{const X=x*r.width,Y=y*r.height;i?ctx.lineTo(X,Y):ctx.moveTo(X,Y)});if(s.length==1)ctx.lineTo(s[0][0]*r.width+.5,s[0][1]*r.height);ctx.stroke()}
  out.value=strokes.length?JSON.stringify(strokes):'';if(hint)hint.style.opacity=strokes.length?0:1}
 const pt=e=>{const r=cv.getBoundingClientRect();return[Math.round(Math.min(Math.max((e.clientX-r.left)/r.width,0),1)*1e4)/1e4,Math.round(Math.min(Math.max((e.clientY-r.top)/r.height,0),1)*1e4)/1e4]};
 cv.addEventListener('pointerdown',e=>{e.preventDefault();cv.setPointerCapture(e.pointerId);cur=[pt(e)];strokes.push(cur);redraw()});
 cv.addEventListener('pointermove',e=>{if(!cur)return;e.preventDefault();for(const ev of (e.getCoalescedEvents?e.getCoalescedEvents():[e]))cur.push(pt(ev));redraw()});
 const end=()=>{cur=null};cv.addEventListener('pointerup',end);cv.addEventListener('pointercancel',end);cv.addEventListener('pointerleave',end);
 form.querySelector('[data-sigclear]').addEventListener('click',()=>{strokes=[];redraw()});
 form.addEventListener('submit',e=>{const n=strokes.reduce((a,s)=>a+s.length,0);if(n<8){e.preventDefault();cv.parentElement.classList.add('ring-rose-400','ring-2');if(hint){hint.textContent='Please draw your signature here first';hint.style.opacity=1;hint.classList.add('text-rose-500')}}});
 window.addEventListener('resize',size);size()})();

/* Heartbeat: turning DRY_RUN off asks for a typed LIVE confirmation. */
(()=>{const d=document.querySelector('[data-dry]');if(!d)return;const box=document.querySelector('[data-livebox]'),was=d.checked;
 const sync=()=>{box.classList.toggle('hidden',d.checked||!was)};d.addEventListener('change',sync);sync()})();

/* Chart tooltips: any [data-tip] inside .chart (hover, focus, touch). */
(()=>{const show=(g,ev)=>{const f=g.closest('.chart'),t=f&&f.querySelector('.chart-tip');if(!t)return;t.textContent=g.dataset.tip;t.classList.remove('hidden');
 const fr=f.getBoundingClientRect(),gr=g.getBoundingClientRect();let x=gr.left-fr.left+gr.width/2-t.offsetWidth/2;x=Math.max(0,Math.min(x,fr.width-t.offsetWidth));t.style.left=x+'px';t.style.top=(gr.top-fr.top-6)+'px'};
 const hide=g=>{const t=g.closest('.chart')?.querySelector('.chart-tip');t&&t.classList.add('hidden')};
 document.addEventListener('mouseover',e=>{const g=e.target.closest&&e.target.closest('.chart [data-tip]');if(g)show(g,e)});
 document.addEventListener('mouseout',e=>{const g=e.target.closest&&e.target.closest('.chart [data-tip]');if(g&&!g.contains(e.relatedTarget))hide(g)});
 document.addEventListener('focusin',e=>{const g=e.target.closest&&e.target.closest('.chart [data-tip]');if(g)show(g,e)});
 document.addEventListener('focusout',e=>{const g=e.target.closest&&e.target.closest('.chart [data-tip]');if(g)hide(g)});})();
document.addEventListener('change',e=>{const f=e.target.closest('[data-submit-on-change]');if(f&&f.files.length)f.form.submit()});
/* Setup wizard: password strength meter (guidance only; the server enforces the rules). */
(()=>{const pw=document.querySelector('[data-strength]');if(!pw)return;const box=document.getElementById(pw.dataset.strength),bars=box.querySelectorAll('[data-bar]'),lab=box.querySelector('[data-label]'),un=document.getElementById(pw.dataset.strengthUser);
 const C=['#e11d48','#f59e0b','#84cc16','#10b981','#10b981'],L=['','Fair','Good','Strong','Very strong'];
 function upd(){const v=pw.value,u=(un&&un.value||'').toLowerCase();let s=0,why='';
  if(v.length<12)why=v?`Too short: ${v.length} of 12 characters`:'';else if(u.length>2&&v.toLowerCase().includes(u))why="Don't include your username";else if(new Set(v).size<5)why='Too repetitive';
  else{s=1;const cls=[/[a-z]/,/[A-Z]/,/\d/,/[^A-Za-z0-9]/].filter(r=>r.test(v)).length;if(v.length>=16)s++;if(cls>=3||v.trim().split(/\s+/).length>=4)s++;if(v.length>=20&&new Set(v).size>=12)s++}
  bars.forEach((b,i)=>{b.style.backgroundColor=v&&i<Math.max(s,1)?C[s]:''});
  lab.textContent=!v?'At least 12 characters. A short sentence works well.':(s?L[s]+(s<3?'. Longer is stronger.':''):why);lab.style.color=v?C[s]:''}
 pw.addEventListener('input',upd);un&&un.addEventListener('input',upd);upd()})();

document.addEventListener('change',e=>{const f=e.target.closest('[data-autosubmit]');if(f&&f.form)f.form.submit()});

document.addEventListener('click',e=>{const o=e.target.closest('[data-open-dialog]');if(o){const d=document.getElementById(o.dataset.openDialog);d&&d.showModal();const i=d&&d.querySelector('input:not([type=hidden])');i&&i.focus()}
 const c=e.target.closest('[data-close-dialog]');if(c){c.closest('dialog').close()}});
document.addEventListener('input',e=>{const i=e.target.closest('[data-match]');if(!i)return;const n=s=>s.trim().replace(/\s+/g,' ').toLowerCase();const b=document.getElementById(i.dataset.enables);if(b)b.disabled=n(i.value)!==n(i.dataset.match)});
document.addEventListener('change',e=>{const w=e.target.closest('[data-who]');if(!w)return;const r=document.querySelector('[data-email-row]');if(r)r.hidden=w.value==='0'});
// live scan progress (person page): poll while a scan is queued or running
(()=>{const box=document.getElementById('scan');if(!box)return;const url=box.dataset.scanStatus,btn=document.querySelector('[data-scan-btn]'),lab=document.querySelector('[data-scan-label]');
 const t=box.querySelector('[data-scan-title]'),bar=box.querySelector('[data-scan-bar]'),ref=box.querySelector('[data-scan-refresh]');
 const f=document.querySelector('[data-scan-form]');if(f)f.addEventListener('submit',()=>{if(btn){btn.disabled=true;lab.textContent='Starting scan…'}});
 const tick=()=>fetch(url,{credentials:'same-origin'}).then(r=>r.json()).then(j=>{
  if(j.state==='queued'||j.state==='running'){box.classList.remove('hidden');const w=/^(new person|onboarding)/.test(j.why||'')?'First scan running':'Scanning';const s=j.total?`${w}… ${j.done} of ${j.total} sites`:(w==='Scanning'?'Starting scan…':'First scan running…');t.textContent=s;lab.textContent=s;btn.disabled=true;
   bar.style.width=(j.total?Math.max(3,Math.round(100*j.done/j.total)):3)+'%';setTimeout(tick,2000)}
  else if(j.state==='done'||j.state==='error'){t.textContent=j.state==='done'?`Scan finished: ${j.found} listing${j.found==1?'':'s'} found`+(j.errors?`, ${j.errors} site error${j.errors==1?'':'s'}`:''):`Scan failed (${j.error||'error'})`;
   bar.style.width='100%';btn.disabled=false;lab.textContent='Scan now';ref.classList.remove('hidden')}}).catch(()=>setTimeout(tick,5000));
 if(['queued','running'].includes(box.dataset.scanState))setTimeout(tick,1500)})();
// screenshot lightbox: zoom, previous/next within a group, download, date and URL
(()=>{const d=document.getElementById('lb');if(!d)return;const img=d.querySelector('[data-lb-img]'),wrap=d.querySelector('[data-lb-wrap]');let list=[],i=0,zoom=false;
 const show=()=>{const b=list[i];img.src=b.dataset.lbSrc;img.alt=b.dataset.lbTitle+' screenshot';d.querySelector('[data-lb-t]').textContent=b.dataset.lbTitle;
  d.querySelector('[data-lb-d]').textContent='Captured '+b.dataset.lbDate;d.querySelector('[data-lb-u]').textContent=b.dataset.lbUrl;d.querySelector('[data-lb-dl]').href=b.dataset.lbDl;
  d.querySelector('[data-lb-n]').textContent=list.length>1?`${i+1} / ${list.length}`:'';d.querySelector('[data-lb-prev]').hidden=d.querySelector('[data-lb-next]').hidden=list.length<2;setZoom(false)};
 const setZoom=z=>{zoom=z;img.classList.toggle('max-w-full',!z);img.classList.toggle('max-h-full',!z);img.classList.toggle('cursor-zoom-in',!z);img.classList.toggle('cursor-zoom-out',z);wrap.classList.toggle('place-items-center',!z)};
 document.addEventListener('click',e=>{const b=e.target.closest('[data-lb]');if(b){e.preventDefault();list=[...document.querySelectorAll(`[data-lb][data-lb-group="${b.dataset.lbGroup}"]`)];i=Math.max(0,list.indexOf(b));show();d.showModal();return}
  if(!d.open)return;if(e.target.closest('[data-lb-close]'))d.close();else if(e.target.closest('[data-lb-next]')){i=(i+1)%list.length;show()}else if(e.target.closest('[data-lb-prev]')){i=(i-1+list.length)%list.length;show()}
  else if(e.target.closest('[data-lb-zoom]')||e.target===img)setZoom(!zoom)});
 document.addEventListener('keydown',e=>{if(!d.open)return;if(e.key==='ArrowRight'){i=(i+1)%list.length;show()}if(e.key==='ArrowLeft'){i=(i-1+list.length)%list.length;show()}})})();

/* ══════════════════════════════════════════════════════════════════════
   AI Fluency Insights — front end
   Motion model: springs, not durations. Every animation starts from the
   value currently on screen, so it can be grabbed and reversed mid-flight.
   ══════════════════════════════════════════════════════════════════════ */

const $=id=>document.getElementById(id);
const fmt=n=>n>=1e9?(n/1e9).toFixed(2)+'B':n>=1e6?(n/1e6).toFixed(2)+'M':n>=1e3?(n/1e3).toFixed(1)+'K':''+n;
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const fmtD=s=>{const p=String(s).split('-');return MON[+p[1]-1]+' '+(+p[2]);};
const fmtMin=m=>m>=60?Math.floor(m/60)+'h'+(m%60?' '+(m%60)+'m':''):m+'m';
const TT=['feature','bugfix','refactor','docs','explore','other'];
const cssv=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const hexa=(h,a)=>{const n=parseInt(h.replace('#',''),16);
return 'rgba('+(n>>16&255)+','+(n>>8&255)+','+(n&255)+','+a+')';};
const RM=matchMedia('(prefers-reduced-motion: reduce)');

let days=30,charts={},D=null,wIdx=null,adoAnim=false,section='overview',customFrom='',customTo='',booted=false;

/* ─────────────────────────── Spring ───────────────────────────
   Apple's two designer parameters: damping ratio and response (seconds).
   Interruptible by construction — `to()` re-targets from the live value
   and carries the current velocity through, so a reversal has no seam. */
class Spring{
  constructor(v,onUpdate,opt){opt=opt||{};
    this.v=v;this.vel=0;this.target=v;this.on=onUpdate;this.raf=0;this.last=0;
    this.zeta=opt.damping===undefined?1:opt.damping;
    this.w=2*Math.PI/(opt.response===undefined?.4:opt.response);}
  jump(t){this.target=this.v=t;this.vel=0;
    if(this.raf){cancelAnimationFrame(this.raf);this.raf=0;}
    this.on(this.v);}
  to(t,vel){
    if(RM.matches){this.jump(t);this.done&&this.done();return;}
    this.target=t;if(vel!==undefined)this.vel=vel;this.run();}
  run(){if(this.raf)return;this.last=performance.now();
    const step=now=>{
      const dt=Math.min(.05,(now-this.last)/1000);this.last=now;
      const n=Math.max(1,Math.ceil(dt/.004)),h=dt/n;
      for(let i=0;i<n;i++){
        const f=-this.w*this.w*(this.v-this.target)-2*this.zeta*this.w*this.vel;
        this.vel+=f*h;this.v+=this.vel*h;}
      this.on(this.v);
      if(Math.abs(this.v-this.target)<.004&&Math.abs(this.vel)<.04){
        this.v=this.target;this.vel=0;this.on(this.v);this.raf=0;
        this.done&&this.done();return;}
      this.raf=requestAnimationFrame(step);};
    this.raf=requestAnimationFrame(step);}
}
/* Apple's momentum projection (exponential decay), not v²/2a */
const projectTo=(v,rate)=>(v/1000)*(rate||.998)/(1-(rate||.998));
/* progressive resistance past a boundary instead of a hard stop */
const rubber=(over,dim,c)=>{c=c||.55;return (over*dim*c)/(dim+c*Math.abs(over));};

/* ─── Selection indicator: 4 independent springs (never one 2D spring) ─── */
function Indicator(thumb){
  let x=0,y=0,w=0,h=0,seeded=false;
  const apply=()=>{thumb.style.transform='translate3d('+x+'px,'+y+'px,0)';
    thumb.style.width=w+'px';thumb.style.height=h+'px';
    thumb.style.visibility='visible';};
  const o={damping:1,response:.36};
  const sx=new Spring(0,v=>{x=v;apply();},o), sy=new Spring(0,v=>{y=v;apply();},o),
        sw=new Spring(0,v=>{w=v;apply();},o), sh=new Spring(0,v=>{h=v;apply();},o);
  return{move(el,instant){if(!el||!el.offsetParent&&!el.offsetWidth)return;
    const t=[el.offsetLeft,el.offsetTop,el.offsetWidth,el.offsetHeight];
    if(!seeded||instant){seeded=true;sx.jump(t[0]);sy.jump(t[1]);sw.jump(t[2]);sh.jump(t[3]);}
    else{sx.to(t[0]);sy.to(t[1]);sw.to(t[2]);sh.to(t[3]);}}};
}
const navInd=Indicator($('navThumb')),rangeInd=Indicator($('rangeThumb'));

/* ─────────────────────────── Appearance ─────────────────────────── */
const SUN='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4.2"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
const MOON='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>';
const AUTO='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 3v18a9 9 0 0 0 0-18z" fill="currentColor" stroke="none"/></svg>';
const THEMES=[['system','System',AUTO],['light','Light',SUN],['dark','Dark',MOON]];
let themeIx=0;
function applyTheme(persist){
  const t=THEMES[themeIx];
  if(t[0]==='system')document.documentElement.removeAttribute('data-theme');
  else document.documentElement.setAttribute('data-theme',t[0]);
  $('themeIcon').innerHTML=t[2];$('themeLabel').textContent=t[1];
  if(persist){try{localStorage.setItem('dtd-theme',t[0]);}catch(e){}}
  themeCharts();if(D)render();paintLegend();
}
try{const sv=localStorage.getItem('dtd-theme');
  const i=THEMES.findIndex(t=>t[0]===sv);if(i>=0)themeIx=i;}catch(e){}
$('themeBtn').onclick=()=>{themeIx=(themeIx+1)%THEMES.length;applyTheme(true);};
matchMedia('(prefers-color-scheme: dark)').addEventListener('change',()=>{
  if(THEMES[themeIx][0]==='system'){themeCharts();if(D)render();paintLegend();}});

/* ─────────────────────────── Chart.js theming ─────────────────────────── */
let C={},PIE=[],GRID={},XGRID={},ANIM={duration:0};
function themeCharts(){
  C={sky:cssv('--sky'),emerald:cssv('--emerald'),violet:cssv('--violet'),amber:cssv('--amber'),
     rose:cssv('--rose'),teal:cssv('--teal'),coral:cssv('--acc'),slate:cssv('--slate'),
     txt:cssv('--txt'),txt2:cssv('--txt-2'),txt3:cssv('--txt-3'),
     grid:cssv('--grid'),fill:cssv('--fill'),hair:cssv('--hair')};
  PIE=[C.coral,C.sky,C.violet,C.emerald,C.amber,C.teal,C.rose];
  GRID={grid:{color:C.grid,drawTicks:false},border:{display:false},ticks:{padding:8}};
  XGRID={grid:{display:false},border:{display:false},
         ticks:{padding:6,maxRotation:0,autoSkip:true,maxTicksLimit:12}};
  const F=cssv('--font');
  Chart.defaults.font.family=F;Chart.defaults.font.size=11;
  Chart.defaults.font.weight=500;
  Chart.defaults.color=C.txt3;Chart.defaults.borderColor=C.grid;
  const L=Chart.defaults.plugins.legend.labels;
  L.usePointStyle=true;L.boxWidth=7;L.boxHeight=7;L.padding=16;L.color=C.txt3;
  const T=Chart.defaults.plugins.tooltip;
  T.backgroundColor=C.fill;T.borderColor=C.hair;T.borderWidth=1;T.padding=11;
  T.cornerRadius=11;T.titleColor=C.txt;T.bodyColor=C.txt2;T.usePointStyle=true;
  T.boxPadding=5;T.displayColors=true;T.titleFont={weight:'600',size:12};
  Chart.defaults.elements.bar.borderRadius=5;Chart.defaults.elements.bar.borderSkipped=false;
  Chart.defaults.elements.point.radius=0;Chart.defaults.elements.point.hoverRadius=5;
  Chart.defaults.elements.point.hitRadius=14;
  Chart.defaults.elements.line.tension=.36;Chart.defaults.elements.line.borderWidth=2;
  Chart.defaults.maintainAspectRatio=false;
  ANIM=RM.matches?{duration:1}:{duration:520,easing:'easeOutQuart'};
}
function mk(id,cfg){const el=$(id);if(!el)return;
  if(charts[id]){charts[id].destroy();delete charts[id];}
  cfg.options=cfg.options||{};
  if(!('animation' in cfg.options))cfg.options.animation=ANIM;
  const ch=charts[id]=new Chart(el,cfg);
  /* Chart.js's construction-time auto-draw can silently no-op (its internal
     animate/update pipeline occasionally never fires the first paint) —
     force one explicit synchronous draw so the chart is never left blank. */
  ch.draw();}
function areaFill(hex){return ctx=>{const ch=ctx.chart,a=ch.chartArea;
  if(!a)return hexa(hex,.14);
  const g=ch.ctx.createLinearGradient(0,a.top,0,a.bottom);
  g.addColorStop(0,hexa(hex,.34));g.addColorStop(1,hexa(hex,0));return g;};}
function paintLegend(){const a=cssv('--acc');
  [['lg1',.14],['lg2',.38],['lg3',.64],['lg4',.92]].forEach(p=>{
    const e=$(p[0]);if(e)e.style.background=hexa(a,p[1]);});}

/* ─────────────────────────── Sparklines ─────────────────────────── */
let spkSeq=0;
function spark(vals,color){
  if(!vals||vals.length<2)return'';
  const w=100,h=26,mx=Math.max.apply(null,vals),mn=Math.min.apply(null,vals),r=(mx-mn)||1;
  const pts=vals.map((v,i)=>[i/(vals.length-1)*w,h-2-((v-mn)/r)*(h-5)]);
  const d=pts.map((p,i)=>(i?'L':'M')+p[0].toFixed(1)+' '+p[1].toFixed(1)).join(' ');
  const id='spk'+(++spkSeq);
  return '<div class="spkwrap"><svg class="spk" viewBox="0 0 '+w+' '+h+'" preserveAspectRatio="none" aria-hidden="true">'+
  '<defs><linearGradient id="'+id+'" x1="0" y1="0" x2="0" y2="1">'+
  '<stop offset="0" stop-color="'+color+'" stop-opacity=".26"/>'+
  '<stop offset="1" stop-color="'+color+'" stop-opacity="0"/></linearGradient></defs>'+
  '<path d="'+d+' L'+w+' '+h+' L0 '+h+' Z" fill="url(#'+id+')"/>'+
  '<path d="'+d+'" fill="none" stroke="'+color+'" stroke-width="1.5" stroke-linecap="round" '+
  'stroke-linejoin="round" vector-effect="non-scaling-stroke"/></svg></div>';
}

/* ─────────────────────────── Icons ─────────────────────────── */
const IC={in:'<path d="M12 3v13m0 0 4-4m-4 4-4-4M4 21h16"/>',
out:'<path d="M12 21V8m0 0 4 4m-4-4-4 4M4 3h16"/>',
cache:'<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
cost:'<path d="M12 1v22M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>',
code:'<path d="M16 18l6-6-6-6M8 6l-6 6 6 6"/>',
prompt:'<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
session:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
alert:'<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
user:'<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
lever:'<path d="M23 6l-9.5 9.5-5-5L1 18"/><path d="M17 6h6v6"/>',
flame:'<path d="M12 2s5 4.5 5 9a5 5 0 0 1-10 0c0-1.2.4-2.3.4-2.3S5 10 5 13a7 7 0 0 0 14 0c0-5.5-7-11-7-11z"/>',
cal:'<rect x="3" y="4" width="18" height="18" rx="2.5"/><path d="M16 2v4M8 2v4M3 10h18"/>',
fix:'<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
layers:'<path d="m12 2 10 5-10 5L2 7z"/><path d="m2 17 10 5 10-5M2 12l10 5 10-5"/>',
target:'<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
check:'<path d="M20 6 9 17l-5-5"/>',
plus:'<path d="M12 5v14M5 12h14"/>',
spark:'<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/>'};
const ic=k=>'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'+IC[k]+'</svg>';

/* ─────────────────────────── Navigation ─────────────────────────── */
const META={
  overview:['Overview','The headline numbers and how they move.'],
  focus:['Time &amp; Focus','When you work, how long, and what it costs you.'],
  tools:['Tools &amp; Models','Which models answered and which tools they reached for.'],
  projects:['Projects','Where the work landed — by repo, branch and file.'],
  weekly:['Weekly Report','Always full history — the range filter does not apply here.'],
  adoption:['My Adoption','How you use Claude Code day to day — last 4 weeks vs the 4 before. The range filter does not apply here.'],
  flow:['Flow','How smoothly work moves: turn time, first-pass success and recovery.'],
  quality:['Quality','Whether speed comes with checking: tests, builds, rework and safety.'],
  value:['Value','What the usage costs and what you get back, at list prices with no guesses.'],
  advisor:['Advisor','Concrete next steps picked from your own data. The range filter does not apply here.'],
  health:['Data Health','What your logs let us measure, and what they cannot. The range filter does not apply here.']};
const NORANGE=['weekly','adoption','advisor','health'];

$('nav').addEventListener('click',e=>{
  const b=e.target.closest('button[data-s]');if(!b)return;setSection(b.dataset.s);});

function setSection(s){
  section=s;
  [...$('nav').querySelectorAll('button[data-s]')].forEach(b=>{
    const on=b.dataset.s===s;b.classList.toggle('on',on);
    b.setAttribute('aria-current',on?'page':'false');
    if(on)navInd.move(b);});
  ['overview','focus','tools','projects','weekly','adoption','flow','quality','value','advisor','health'].forEach(k=>{
    const el=$('s-'+k);el.hidden=k!==s;
    if(k===s){el.classList.remove('enter');void el.offsetWidth;el.classList.add('enter');
      setTimeout(()=>el.classList.remove('enter'),900);}});
  $('actsData').style.display=NORANGE.indexOf(s)>=0?'none':'flex';
  $('actsWeek').style.display=s==='weekly'?'flex':'none';
  if(s==='adoption')adoAnim=true;
  updateChrome();
  if(D)render();
  if(NORANGE.indexOf(s)<0)requestAnimationFrame(()=>rangeInd.move($('range').querySelector('.on'),true));
}
function rangeText(){
  if(customFrom||customTo)return (customFrom||'the start')+' → '+(customTo||'today');
  return days?('Last '+days+' days'):'All time';}
function updateChrome(){
  const m=META[section];$('secTitle').innerHTML=m[0];
  let sub=m[1];
  if(NORANGE.indexOf(section)>=0&&section!=='weekly'){}
  else if(section!=='weekly')sub=rangeText()+' · '+sub;
  else if(D&&D.weeks&&D.weeks.length&&wIdx!==null&&D.weeks[wIdx])
    sub='Week of '+fmtD(D.weeks[wIdx].start)+' – '+fmtD(D.weeks[wIdx].end)+' · '+sub;
  $('secSub').innerHTML=sub;}

/* ─────────────────────────── Range controls ─────────────────────────── */
$('range').addEventListener('click',e=>{
  const b=e.target.closest('button[data-d]');if(!b)return;
  days=+b.dataset.d;customFrom='';customTo='';
  $('cFrom').value='';$('cTo').value='';
  [...$('range').querySelectorAll('button')].forEach(x=>x.classList.remove('on'));
  b.classList.add('on');$('rangeThumb').style.opacity=1;
  rangeInd.move(b);updateChrome();load();});

/* custom-range popover — scales out of its own trigger, not the page centre */
const popS=new Spring(0,v=>{const p=$('cPop');
  p.style.opacity=v;p.style.transform='scale('+(.92+.08*v)+') translateY('+((1-v)*-5)+'px)';},
  {damping:1,response:.3});
let popOpen=false;
popS.done=()=>{if(!popOpen&&popS.target===0)$('cPop').classList.remove('open');};
function togglePop(open){
  popOpen=open;const p=$('cPop');
  if(open){p.classList.add('open');popS.to(1);}else{popS.to(0);
    if(RM.matches)p.classList.remove('open');}}
$('cBtn').onclick=e=>{e.stopPropagation();togglePop(!popOpen);};
$('cPop').onclick=e=>e.stopPropagation();
document.addEventListener('click',()=>{if(popOpen)togglePop(false);});
$('cApply').onclick=()=>{
  const f=$('cFrom').value,t=$('cTo').value;if(!f&&!t)return;
  customFrom=f;customTo=t;
  [...$('range').querySelectorAll('button')].forEach(x=>x.classList.remove('on'));
  $('rangeThumb').style.opacity=0;          // no segment owns the selection now
  togglePop(false);updateChrome();load();};
$('cClear').onclick=()=>{
  customFrom='';customTo='';$('cFrom').value='';$('cTo').value='';
  const b=$('range').querySelector('[data-d="'+days+'"]');
  $('rangeThumb').style.opacity=1;
  if(b){b.classList.add('on');rangeInd.move(b);}
  togglePop(false);updateChrome();load();};

/* ─────────────────────────── Scroll edge ─────────────────────────── */
let edgeTick=false;
addEventListener('scroll',()=>{if(edgeTick)return;edgeTick=true;
  requestAnimationFrame(()=>{edgeTick=false;
    document.documentElement.style.setProperty('--sc',Math.min(1,scrollY/26).toFixed(3));});},
  {passive:true});
addEventListener('resize',()=>{
  navInd.move($('nav').querySelector('.on'),true);
  const rb=$('range').querySelector('.on');if(rb)rangeInd.move(rb,true);});

/* ─────────────────────────── Data ─────────────────────────── */
async function load(){
  const qs=(customFrom||customTo)
    ?('from='+encodeURIComponent(customFrom)+'&to='+encodeURIComponent(customTo))
    :('days='+days);
  let data;
  try{
    const r=await fetch('/api/stats?'+qs);
    if(!r.ok)throw new Error('HTTP '+r.status);
    data=await r.json();
  }catch(err){
    console.error('Failed to load dashboard data:',err);
    let cw=$('connwarn');
    if(!cw){cw=document.createElement('div');cw.id='connwarn';cw.className='warn';
      $('alerts').appendChild(cw);}
    cw.innerHTML=ic('alert')+'<span>Can’t reach the dashboard server — retrying every 15s. '+
      (D?'Showing the last data loaded.':'')+'</span>';
    return;   // keep whatever was last on screen; setInterval(load,...) will retry
  }
  const cw=$('connwarn');if(cw)cw.remove();
  D=data;
  $('upd').textContent=D.generated;
  $('root').textContent=D.root||'~/.claude/projects';
  let pw=$('parsewarn');
  if(D.totals.parse_errors>0){
    if(!pw){pw=document.createElement('div');pw.id='parsewarn';pw.className='warn';
      $('alerts').appendChild(pw);}
    pw.innerHTML=ic('alert')+'<span>'+
      D.totals.parse_errors.toLocaleString()+
      ' log entries could not be parsed — the log format may have changed; stats may be incomplete.</span>';
  }else if(pw){pw.remove();}
  if(D.notifications&&D.notifications.length){
    const seen=window.__notifySeen||(window.__notifySeen=new Set());
    D.notifications.forEach(n=>{
      if(seen.has(n.id))return;
      seen.add(n.id);
      const el=document.createElement('div');
      el.className='warn notify';
      el.innerHTML=ic('alert')+'<span>'+esc(n.project)+' — '+esc(n.title)+': '+
        esc(n.message)+'</span><button class="notifyX" aria-label="Dismiss">&times;</button>';
      el.querySelector('.notifyX').onclick=()=>el.remove();
      $('alerts').appendChild(el);
      setTimeout(()=>el.remove(),20000);
    });
  }
  if(wIdx===null&&D.weeks.length)wIdx=D.weeks.length-1;
  if(wIdx!==null&&wIdx>=D.weeks.length)wIdx=D.weeks.length-1;
  updateChrome();render();
  if(!booted){booted=true;
    requestAnimationFrame(()=>{navInd.move($('nav').querySelector('.on'),true);
      rangeInd.move($('range').querySelector('.on'),true);});}
}
function render(){
  ({overview:renderOverview,focus:renderFocus,tools:renderTools,
    projects:renderProjects,weekly:renderWeek,adoption:renderAdoption,
    flow:renderFlow,quality:renderQuality,value:renderValue,advisor:renderAdvisor,health:renderHealth}[section])();}

function delta(cur,prev){
  if(prev==null||prev===0||!isFinite(cur/prev))return'';
  const pc=Math.round((cur-prev)/prev*100);
  if(pc===0)return'<span class="flat-d">no change vs last period</span>';
  return '<span class="'+(pc>0?'up':'down')+'">'+(pc>0?'▲':'▼')+' '+
    Math.abs(pc)+'% vs last period</span>';}

/* ─────────────────────────── Overview ─────────────────────────── */
function renderMomentum(){
  const card=$('momCard');
  if(D.streak===undefined){card.style.display='none';return;}
  card.style.display='';
  const g=D.goals||{},today=D.today||{};
  let h='<div class="mblock"><div class="mico">'+ic('flame')+'</div><div>'+
    '<div class="num-xl">'+D.streak+'</div><div class="ml">day streak · best '+D.best_streak+'</div></div></div>'+
    '<div class="vsep"></div>'+
    '<div class="mblock"><div class="mico">'+ic('cal')+'</div><div>'+
    '<div class="num-xl">'+D.active_days+'</div><div class="ml">active days all-time</div></div></div>';
  const bars=[];
  const bar=(label,val,goal)=>{if(!goal)return;
    const pc=Math.min(100,Math.round(val/goal*100)),over=val>goal;
    bars.push('<div class="goalw"><div class="gt"><span>'+label+'</span><b>'+fmt(val)+' / '+fmt(goal)+
      '</b></div><div class="bar'+(over?' over':'')+'"><i style="width:'+pc+'%"></i></div></div>');};
  bar("Today's LoC",today.loc||0,g.daily_loc);
  bar("Today's output",today.tokens||0,g.daily_tokens);
  bar('This week output',D.week_tokens||0,g.weekly_tokens);
  if(bars.length)h+='<div class="goals">'+bars.join('')+'</div>';
  $('mom').innerHTML=h;}

function renderPlanUsage(){
  const card=$('planCard'),pw=D.plan_window;
  const active=pw&&(pw.source==='official'||pw.ceiling);
  if(!active){card.style.display='none';return;}
  card.style.display='';
  const isOfficial=pw.source==='official';
  const barRow=(title,pct,mins)=>{
    const p=Math.min(100,pct),over=pct>=100,hasMins=mins!=null;
    const rh=hasMins?Math.floor(mins/60):0,rm=hasMins?mins%60:0;
    return '<div class="planrow"><div class="plancol"><div class="pcs">'+title+'</div>'+
      (hasMins?'<div class="pcr">Resets in '+rh+'h '+rm+'m</div>':'')+'</div>'+
      '<div class="bar planbar'+(over?' over':'')+'"><i style="width:'+p+'%"></i></div>'+
      '<div class="pcpct">'+pct+'% used</div></div>';};
  let rows=barRow('Current session',pw.pct,pw.resets_in_min);
  if(isOfficial&&pw.week_pct!=null)rows+=barRow('This week',pw.week_pct,pw.week_resets_in_min);
  // The statusline only re-captures on Claude Code activity (see statusline.js),
  // so a gap here means no session on this machine has made an API call
  // recently -- the number shown is real, just not necessarily current.
  // Above STALE_AGE_MIN, say so loudly rather than a quiet gray hint.
  const STALE_AGE_MIN=2;
  const fmtAge=m=>m<60?m+'m':Math.floor(m/60)+'h '+(m%60)+'m';
  let age='';
  if(isOfficial&&pw.captured_age_min!=null){
    const stale=pw.captured_age_min>=STALE_AGE_MIN;
    age=' <span class="hint'+(stale?' stale':'')+'" title="Updates only when a Claude Code session on this machine is active. It can lag the live /usage number during idle stretches.">'+
      (stale?'⚠ last updated '+fmtAge(pw.captured_age_min)+' ago, may be behind'
            :(pw.captured_age_min<1?'as of just now':'as of '+fmtAge(pw.captured_age_min)+' ago'))+
      '</span>';
  }
  $('planUsage').innerHTML=
    '<div class="planw"><div class="plant">Plan usage limits<span class="hint">'+
    (isOfficial?'(live)':'(estimate)')+'</span>'+age+'</div>'+rows+'</div>';}

function renderOverview(){
  const d=D,t=d.totals;
  renderMomentum();
  renderPlanUsage();
  const S=k=>d.days.map(x=>x[k]||0);
  const kpis=[
    ['Input tokens',fmt(t.input),'sent to Claude','in',C.sky,t.input,'input',S('input')],
    ['Output tokens',fmt(t.output),'generated by Claude','out',C.emerald,t.output,'output',S('output')],
    ['Cache read',fmt(t.cache_read),
      (t.cache_reset_pct>5?'⚠ cache resetting mid-session (~'+t.cache_reset_pct+'% of turns)'
                          :t.cache_efficiency+'% cache hit rate'),
      'cache',C.teal,t.cache_read,'cache_read',S('cache_read')],
    ['Est. API cost','$'+t.cost,'$0 on a subscription plan','cost',C.amber,t.cost,'cost',S('cost')],
    ['Cold-start tax',t.cold_start_pct+'%','of cost is priming the cache on a session’s first call','cost',C.rose,t.cold_start_pct,'cold_start_pct',null],
    ['Lines of code',fmt(t.loc),'written by Claude','code',C.violet,t.loc,'loc',S('loc')],
    ['Prompts',fmt(t.prompts),t.avg_prompt_lines+' lines avg','prompt',C.coral,t.prompts,'prompts',S('prompts')],
    ['Sessions',t.sessions,t.avg_session_min+' min avg','session',C.sky,t.sessions,'sessions',null],
    ['Friction',t.interruptions,t.tool_errors+' tool errors','alert',C.rose,null,null,
      d.days.map(x=>(x.interruptions||0)+(x.tool_errors||0))],
    ['Corrections',fmt(t.corrections),(t.prompts?Math.round(t.corrections/t.prompts*100):0)+'% of prompts','fix',C.rose,t.corrections,'corrections',S('corrections')],
    ['Your tokens',fmt(t.human_tokens),'typed in prompts (est.)','user',C.amber,t.human_tokens,'human_tokens',S('human_tokens')],
    ['Leverage',t.leverage+'×','Claude tokens per typed token','lever',C.emerald,null,null,null]];
  $('kpis').innerHTML=kpis.map(k=>{
    const dh=(k[6]&&D.prev)?delta(k[5],D.prev[k[6]]):'';
    const sp=k[7]&&k[7].some(v=>v>0)?spark(k[7],k[4]):'';
    return '<div class="kpi'+(sp?'':' plain')+'" style="--kc:'+k[4]+';--kbg:'+hexa(k[4],.13)+'">'+
      '<div class="khead"><span class="eyebrow">'+k[0]+'</span><span class="ic">'+ic(k[3])+'</span></div>'+
      '<div class="num-lg">'+k[1]+'</div>'+
      (dh?'<div class="kd">'+dh+'</div>':'')+
      '<div class="s">'+k[2]+'</div>'+sp+'</div>';}).join('');

  const labels=d.days.map(x=>x.day.slice(5));
  mk('cTokens',{type:'bar',data:{labels,datasets:[
    {label:'Input',data:S('input'),backgroundColor:C.sky},
    {label:'Output',data:S('output'),backgroundColor:C.emerald},
    {label:'Cache read',data:S('cache_read'),backgroundColor:C.slate}]},
    options:{scales:{x:{stacked:true,...XGRID},y:{stacked:true,...GRID}},
      plugins:{legend:{position:'bottom'}},interaction:{mode:'index',intersect:false}}});
  mk('cBalance',{type:'line',data:{labels,datasets:[
    {label:'You (typed)',data:S('human_tokens'),borderColor:C.amber,backgroundColor:areaFill(C.amber),fill:true},
    {label:'Claude (output)',data:S('output'),borderColor:C.coral,backgroundColor:areaFill(C.coral),fill:true}]},
    options:{scales:{x:XGRID,y:{type:'logarithmic',...GRID}},
      plugins:{legend:{position:'bottom'}},interaction:{mode:'index',intersect:false}}});
  mk('cLoc',{type:'line',data:{labels,datasets:[{label:'LoC',data:S('loc'),
    borderColor:C.violet,backgroundColor:areaFill(C.violet),fill:true}]},
    options:{scales:{x:XGRID,y:GRID},plugins:{legend:{display:false}}}});
  mk('cRW',{type:'bar',data:{labels,datasets:[
    {label:'Exploration (reads/searches)',data:S('reads'),backgroundColor:C.sky},
    {label:'Building (file edits)',data:S('writes'),backgroundColor:C.emerald}]},
    options:{scales:{x:{stacked:true,...XGRID},y:{stacked:true,...GRID}},
      plugins:{legend:{position:'bottom'}},interaction:{mode:'index',intersect:false}}});
}

/* ─────────────────────────── Time & Focus ─────────────────────────── */
function renderFocus(){
  const d=D,labels=d.days.map(x=>x.day.slice(5));
  const sum=k=>d.days.reduce((a,x)=>a+(x[k]||0),0);
  const actTot=sum('active_min'),msgTot=sum('messages'),lateTot=sum('late_msgs');
  const focusMax=d.days.reduce((a,x)=>Math.max(a,x.focus_max||0),0);
  const wknd=d.days.reduce((a,x)=>{const g=new Date(x.day+'T12:00:00').getDay();
    return a+((g===0||g===6)?x.messages:0);},0);
  const actDays=d.days.filter(x=>x.messages>0).length;
  const wf=[[fmtMin(actTot),'active time in range'],
    [actDays?fmtMin(Math.round(actTot/actDays)):'0m','avg per active day'],
    [fmtMin(focusMax),focusMax>=180?'longest focus block — take breaks':'longest focus block'],
    [(msgTot?Math.round(lateTot/msgTot*100):0)+'%','in quiet hours (23–06)'],
    [(msgTot?Math.round(wknd/msgTot*100):0)+'%','on weekends']];
  $('wellFacts').innerHTML=wf.map(f=>
    '<div><div class="num-lg">'+f[0]+'</div><div class="fl">'+f[1]+'</div></div>').join('');
  mk('cActive',{type:'bar',data:{labels,datasets:[{label:'Active minutes',
    data:d.days.map(x=>x.active_min||0),
    backgroundColor:d.days.map(x=>((x.late_msgs||0)>x.messages*.25&&x.messages)?C.rose:C.teal)}]},
    options:{scales:{x:XGRID,y:GRID},plugins:{legend:{display:false},tooltip:{callbacks:{
      label:c=>fmtMin(c.parsed.y)+' active'+
        ((d.days[c.dataIndex].late_msgs||0)>d.days[c.dataIndex].messages*.25?' · heavy quiet-hours use':'')}}}}});

  const byDay={};d.days.forEach(x=>byDay[x.day]=Object.fromEntries(TT.map(t=>[t,0])));
  (D.sessions_list||[]).forEach(s=>{if(byDay[s.day]&&s.type)byDay[s.day][s.type]++;});
  const TC={feature:C.emerald,bugfix:C.rose,refactor:C.violet,docs:C.amber,explore:C.sky,other:C.slate};
  mk('cTaskMix',{type:'bar',data:{labels,datasets:TT.map(t=>
    ({label:t,data:d.days.map(x=>byDay[x.day][t]),backgroundColor:TC[t]}))},
    options:{scales:{x:{stacked:true,...XGRID},y:{stacked:true,...GRID,ticks:{precision:0}}},
      plugins:{legend:{position:'bottom'}},interaction:{mode:'index',intersect:false}}});
  mk('cFrict',{type:'bar',data:{labels,datasets:[
    {label:'Interruptions',data:d.days.map(x=>x.interruptions),backgroundColor:C.rose},
    {label:'Tool errors',data:d.days.map(x=>x.tool_errors),backgroundColor:C.amber}]},
    options:{scales:{x:{stacked:true,...XGRID},y:{stacked:true,...GRID}},
      plugins:{legend:{position:'bottom'}},interaction:{mode:'index',intersect:false}}});

  const wd=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
  const max=Math.max(1,...d.heatmap.flat()),acc=cssv('--acc'),base=cssv('--fill-3');
  let hm='<div></div>'+[...Array(24).keys()].map(h=>'<div class="hh">'+(h%2?'':h)+'</div>').join('');
  d.heatmap.forEach((row,i)=>{hm+='<div class="wd">'+wd[i]+'</div>'+row.map((v,h)=>
    '<div class="cell" title="'+wd[i]+' '+h+':00 — '+v+' message'+(v===1?'':'s')+
    '" style="background:'+(v?hexa(acc,(.14+.78*v/max).toFixed(3)):base)+'"></div>').join('');});
  $('heat').innerHTML=hm;paintLegend();
}

/* ─────────────────────────── Tools & Models ─────────────────────────── */
function renderTools(){
  const d=D;
  mk('cModels',{type:'doughnut',data:{labels:d.models.map(m=>m.name),
    datasets:[{data:d.models.map(m=>m.output),backgroundColor:PIE,
      borderColor:cssv('--fill'),borderWidth:3,hoverOffset:8}]},
    options:{cutout:'68%',plugins:{legend:{position:'bottom'}}}});
  const AUTO_LBL={auto:'Auto (full autonomy)',default:'Default (asks each time)',
    acceptEdits:'Accept edits (asks before commands)',unknown:'Unknown'};
  const AUTO_CLR={auto:C.emerald,default:C.sky,acceptEdits:C.violet,unknown:C.slate};
  const pm=d.permission_modes||[];
  mk('cAutonomy',{type:'doughnut',data:{labels:pm.map(x=>AUTO_LBL[x[0]]||x[0]),
    datasets:[{data:pm.map(x=>x[1]),backgroundColor:pm.map(x=>AUTO_CLR[x[0]]||C.slate),
      borderColor:cssv('--fill'),borderWidth:3,hoverOffset:8}]},
    options:{cutout:'68%',plugins:{legend:{position:'bottom'}}}});
  const sub=d.subagents||[];
  mk('cDelegation',{type:'doughnut',data:{labels:sub.map(x=>x[0]),
    datasets:[{data:sub.map(x=>x[1]),backgroundColor:PIE,
      borderColor:cssv('--fill'),borderWidth:3,hoverOffset:8}]},
    options:{cutout:'68%',plugins:{legend:{position:'bottom'}}}});
  mk('cTools',{type:'bar',data:{labels:d.tools.map(x=>x[0]),
    datasets:[{data:d.tools.map(x=>x[1]),backgroundColor:C.coral,borderRadius:5}]},
    options:{indexAxis:'y',scales:{x:GRID,y:XGRID},plugins:{legend:{display:false}}}});
  mk('cSlash',{type:'bar',data:{labels:d.slash.map(x=>x[0]),
    datasets:[{data:d.slash.map(x=>x[1]),backgroundColor:C.teal,borderRadius:5}]},
    options:{indexAxis:'y',scales:{x:GRID,y:XGRID},plugins:{legend:{display:false}}}});
  document.querySelector('#tModels tbody').innerHTML=d.models.length?d.models.map(m=>
    '<tr><td><b>'+esc(m.name)+'</b></td><td class="num">'+m.msgs+'</td><td class="num">'+
    fmt(m.input)+'</td><td class="num">'+fmt(m.output)+'</td><td class="num">'+m.cost+'</td></tr>').join('')
    :'<tr><td colspan="5" class="empty">No model activity in this range.</td></tr>';
}

/* ─────────────────────────── Projects ─────────────────────────── */
const ARROW='<svg class="rowarrow" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg>';
function renderProjects(){
  const d=D;
  document.querySelector('#tProj tbody').innerHTML=d.projects.length?d.projects.map(p=>
    '<tr tabindex="0"><td title="'+esc(p.name)+'"><b>'+esc(p.name.split('/').pop()||p.name)+
    '</b></td><td class="num">'+fmt(p.input)+'</td><td class="num">'+fmt(p.output)+
    '</td><td class="num">'+fmt(p.loc)+'</td><td class="num">'+p.cost+
    '</td><td>'+(p.has_instructions?'<span class="up">&#10003; CLAUDE.md</span>':'<span class="flat-d">&mdash;</span>')+
    '</td><td class="num">'+ARROW+'</td></tr>').join('')
    :'<tr><td colspan="7" class="empty">No projects in this range.</td></tr>';
  document.querySelectorAll('#tProj tbody tr').forEach((tr,i)=>{
    if(!d.projects[i])return;
    tr.onclick=()=>openProject(d.projects[i]);
    tr.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();openProject(d.projects[i]);}};});
  document.querySelector('#tBranch tbody').innerHTML=d.branches.length?d.branches.map(b=>
    '<tr><td><b>'+esc(b.name)+'</b></td><td class="num">'+b.msgs+'</td><td class="num">'+
    fmt(b.output)+'</td><td class="num">'+fmt(b.loc)+'</td></tr>').join('')
    :'<tr><td colspan="4" class="empty">No branch data in this range.</td></tr>';
  document.querySelector('#tFiles tbody').innerHTML=d.top_files.length?d.top_files.map(f=>
    '<tr><td>'+esc(pv(f[0]))+'</td><td class="num">'+fmt(f[1])+'</td></tr>').join('')
    :'<tr><td colspan="2" class="empty">No files edited in this range.</td></tr>';
  $('lPrompts').innerHTML=d.longest_prompts.length?d.longest_prompts.map(p=>
    '<li>'+esc(pv(p.preview))+'…<div class="mt"><b>'+p.lines+' lines</b> · '+p.words+
    ' words · '+p.day+'</div></li>').join('')
    :'<li class="empty">No prompts in this range.</li>';
}

/* ─────────────────────── Engineering insight tabs ─────────────────────── */
let PRIV=false;
try{PRIV=localStorage.getItem('dtd_priv')==='1';}catch(e){}
const pv=s=>PRIV?'•••':s;
function setPriv(v){PRIV=v;try{localStorage.setItem('dtd_priv',v?'1':'0');}catch(e){}
  if(D)render();paintPriv();}
function paintPriv(){const b=$('privBtn');if(b)b.textContent=PRIV?'Privacy mode: on':'Privacy mode: off';}

const fmtS=s=>s==null?'–':s<10?s.toFixed(1)+'s':s<60?Math.round(s)+'s':s<3600?Math.floor(s/60)+'m '+Math.round(s%60)+'s':Math.floor(s/3600)+'h '+Math.round(s%3600/60)+'m';
const CONF={observed:'Observed',derived:'Derived',proxy:'Proxy',estimated:'Estimated'};
function cb(id){const m=D&&D.catalog&&D.catalog[id];if(!m)return'';
  return '<span class="cb '+m.confidence+'" title="'+esc(m.name+' · '+CONF[m.confidence]+'. '+m.how+' Limit: '+m.limit)+'">'+CONF[m.confidence]+'</span>';}
function insDelta(cur,prev,lowerBetter,f){
  if(cur==null||prev==null)return'';
  const d=cur-prev;if(Math.abs(d)<1e-9)return'<span class="flat-d">same as previous period</span>';
  const good=lowerBetter?d<0:d>0;
  return '<span class="'+(good?'up':'down')+'">'+(d>0?'▲ ':'▼ ')+(f?f(Math.abs(d)):Math.abs(Math.round(d*10)/10))+' vs previous period</span>';}
function tile(label,val,sub,color,cid,dl){
  return '<div class="kpi plain" style="--kc:'+color+';--kbg:'+hexa(color,.13)+'"><div class="khead"><span class="eyebrow">'+label+'</span>'+(cid?cb(cid):'')+
    '</div><div class="num-lg">'+val+'</div>'+(dl?'<div class="kd">'+dl+'</div>':'')+'<div class="s">'+sub+'</div></div>';}
function ringHtml(score,gid){
  const R=52,CIRC=2*Math.PI*R,off=CIRC*(1-score/100);
  return '<div class="ad-ring sm" style="--circ:'+CIRC.toFixed(1)+'"><svg viewBox="0 0 120 120"><defs><linearGradient id="'+gid+'" x1="0" y1="0" x2="1" y2="1">'+
    '<stop offset="0" style="stop-color:var(--acc)"/><stop offset="1" style="stop-color:var(--acc-2)"/></linearGradient></defs>'+
    '<circle class="trk" cx="60" cy="60" r="'+R+'"/><circle class="val" cx="60" cy="60" r="'+R+'" stroke="url(#'+gid+')" stroke-dasharray="'+CIRC.toFixed(1)+'" stroke-dashoffset="'+off.toFixed(1)+'"/></svg>'+
    '<div class="mid"><div class="big">'+score+'</div><div class="of">out of 100</div></div></div>';}
function insHero(el,gid,eyebrow,cid,score,words,sentence,tiles,emptyMsg){
  if(score==null){
    el.innerHTML='<div class="ins-left"><div><div class="eyebrow">'+eyebrow+'</div><h2>Not enough data yet</h2><div class="ins-empty" style="margin-top:6px">'+emptyMsg+'</div></div></div><div class="ins-tiles">'+tiles+'</div>';return;}
  el.innerHTML='<div class="ins-left">'+ringHtml(score,gid)+'<div><div class="eyebrow">'+eyebrow+'</div><h2>'+words+'</h2><div class="d">'+sentence+'</div>'+cb(cid)+'</div></div><div class="ins-tiles">'+tiles+'</div>';}
const band=(sc,w)=>sc>=80?w[0]:sc>=60?w[1]:sc>=40?w[2]:w[3];
const P=(n)=>n==null?'–':n+'%';
const dpts=v=>Math.round(v)+' pts';

function renderFlow(){
  const f=D.flow,pr=(D.prev_insight||{}).flow||{};
  const tiles=
    tile('Median turn',fmtS(f.turn_median_s),'slowest 10%: '+fmtS(f.turn_p90_s),C.sky,'turn_time',insDelta(f.turn_median_s,pr.turn_median_s,true,fmtS))+
    tile('Clean first-pass turns',P(f.clean_pct),f.corrected+' corrected · '+f.interrupted+' interrupted · '+f.with_errors+' with errors',C.emerald,'clean_turns',insDelta(f.clean_pct,pr.clean_pct,false,dpts))+
    tile('Failures fixed',P(f.recovery_pct),f.episodes?f.recovered+' of '+f.episodes+' failures recovered · '+f.stuck+' stuck loop'+(f.stuck===1?'':'s'):'no failures in this range',C.amber,'recovery',insDelta(f.recovery_pct,pr.recovery_pct,false,dpts))+
    tile('Time to first edit',fmtS(f.first_edit_median_s),f.first_edit_n+' session'+(f.first_edit_n===1?'':'s')+' with edits',C.violet,'first_edit',insDelta(f.first_edit_median_s,pr.first_edit_median_s,true,fmtS));
  insHero($('flowHero'),'flowGrad','Flow score','flow_score',f.score,band(f.score||0,['Strong flow','Mostly smooth','Bumpy','Rough going']),
    f.clean_pct+'% of your turns landed first time, and '+(f.recovery_pct==null?'no failures needed fixing':f.recovery_pct+'% of failures got fixed')+'.',
    tiles,'Needs 5 timed turns in this range; you have '+f.turns+'. Pick a longer range or keep working.');
  const lb=f.series.map(x=>x.day.slice(5));
  mk('cFlowTurn',{type:'line',data:{labels:lb,datasets:[
    {label:'Median',data:f.series.map(x=>x.median),borderColor:C.sky,backgroundColor:areaFill(C.sky),fill:true},
    {label:'Slowest 10%',data:f.series.map(x=>x.p90),borderColor:C.slate,borderDash:[4,4],fill:false}]},
    options:{scales:{x:XGRID,y:{...GRID,ticks:{padding:8,callback:v=>fmtS(v)}}},plugins:{legend:{position:'bottom'},tooltip:{callbacks:{label:c=>c.dataset.label+': '+fmtS(c.parsed.y)}}},interaction:{mode:'index',intersect:false}}});
  mk('cFlowClean',{type:'bar',data:{labels:lb,datasets:[{label:'Clean turns %',data:f.series.map(x=>x.clean_pct),backgroundColor:C.emerald}]},
    options:{scales:{x:XGRID,y:{min:0,max:100,...GRID}},plugins:{legend:{display:false}}}});
  $('cbTurn').innerHTML=cb('turn_time');$('cbClean').innerHTML=cb('clean_turns')+' A correction is read from the start of your next prompt.';
  $('cbTool').innerHTML=cb('tool_time');$('cbRec').innerHTML=cb('recovery');
  $('flowToolNote').textContent=f.tool_share_pct==null?'':'Tools account for about '+f.tool_share_pct+'% of your turn time. The rest is Claude thinking and writing, plus any wait for your approval.';
  const tl=f.tools.slice(0,8);
  mk('cFlowTools',{type:'bar',data:{labels:tl.map(t=>t.family),datasets:[{label:'Share of tool time %',data:tl.map(t=>t.share_pct),backgroundColor:C.coral}]},
    options:{indexAxis:'y',scales:{x:{...GRID,ticks:{padding:6,callback:v=>v+'%'}},y:{grid:{display:false},border:{display:false}}},plugins:{legend:{display:false}}}});
  document.querySelector('#tFlowTools tbody').innerHTML=f.tools.length?f.tools.map(t=>
    '<tr><td><b>'+esc(t.family)+'</b></td><td class="num">'+t.calls+'</td><td class="num">'+fmtS(t.median_s)+'</td><td class="num">'+fmtS(t.p90_s)+'</td><td class="num">'+fmtS(t.max_s)+'</td><td class="num">'+t.share_pct+'%</td></tr>').join('')
    :'<tr><td colspan="6" class="empty">No timed tool calls in this range.</td></tr>';
  $('flowRecFacts').innerHTML=
    '<div><div class="num-lg">'+f.episodes+'</div><div class="fl">failure episodes</div></div>'+
    '<div><div class="num-lg">'+f.recovered+'</div><div class="fl">recovered</div></div>'+
    '<div><div class="num-lg">'+(f.recover_steps_median==null?'–':f.recover_steps_median)+'</div><div class="fl">steps to recover (median)</div></div>'+
    '<div><div class="num-lg">'+f.stuck+'</div><div class="fl">stuck loops (3+ in a row)</div></div>';
  $('flowFailTools').innerHTML=f.fail_tools.length?'<span class="chip2">Fails most:</span>'+f.fail_tools.map(x=>'<span class="chip2"><b>'+esc(x.tool)+'</b> ×'+x.episodes+'</span>').join(''):'';
  $('flowShapeFacts').innerHTML=
    '<div><div class="num-lg">'+(f.session_median_min==null?'–':fmtMin(Math.round(f.session_median_min)))+'</div><div class="fl">typical session '+cb('session_len')+'</div></div>'+
    '<div><div class="num-lg">'+(f.session_p90_min==null?'–':fmtMin(Math.round(f.session_p90_min)))+'</div><div class="fl">longest 10% of sessions</div></div>'+
    '<div><div class="num-lg">'+fmtS(f.thinking_median_s)+'</div><div class="fl">median thinking time '+cb('thinking')+'</div></div>'+
    '<div><div class="num-lg">'+(f.tool_share_pct==null?'–':f.tool_share_pct+'%')+'</div><div class="fl">of turn time in tools</div></div>';
  const tot=f.effort.reduce((a,x)=>a+x.n,0),ec=[C.coral,C.sky,C.violet,C.teal];
  $('flowEffortBar').innerHTML=tot?f.effort.map((x,i)=>'<i style="width:'+(x.n/tot*100)+'%;background:'+ec[i%4]+'" title="'+esc(x.level)+'"></i>').join(''):'';
  $('flowEffortChips').innerHTML=tot?f.effort.map((x,i)=>'<span class="chip2"><b style="color:'+ec[i%4]+'">●</b> '+esc(x.level)+' <b>'+Math.round(x.n/tot*100)+'%</b></span>').join(''):'<span class="chip2">Effort level is not in your logs.</span>';
}

function renderQuality(){
  const q=D.quality,pr=(D.prev_insight||{}).quality||{};
  const tiles=
    tile('Verified after last edit',P(q.verified_pct),q.verified+' of '+q.edited_sessions+' edit sessions ended with a check',C.emerald,'verified',insDelta(q.verified_pct,pr.verified_pct,false,dpts))+
    tile('First-run pass rate',P(q.first_run_pass_pct),q.run_groups?q.run_groups+' session-and-check pairs':'no test, build or lint runs',C.sky,'first_run',insDelta(q.first_run_pass_pct,pr.first_run_pass_pct,false,dpts))+
    tile('Tests alongside code',P(q.tests_alongside_pct),q.with_tests+' of '+q.edited_sessions+' edit sessions touched a test file',C.violet,'tests_alongside',insDelta(q.tests_alongside_pct,pr.tests_alongside_pct,false,dpts))+
    tile('Re-edit share',P(q.reedit_pct),'of '+q.edits+' edits revisit a file edited earlier in the session',C.amber,'reedit',insDelta(q.reedit_pct,pr.reedit_pct,true,dpts));
  insHero($('qualHero'),'qualGrad','Quality score','quality_score',q.score,band(q.score||0,['Well checked','Reasonably checked','Lightly checked','Rarely checked']),
    q.verified_pct+'% of edit sessions ended with a test, build or lint, and '+q.tests_alongside_pct+'% touched a test file.',
    tiles,'Needs 3 sessions where Claude edited files in this range; you have '+q.edited_sessions+'.');
  const rk=Object.values(q.runs),KL={test:'Tests',build:'Builds',lint:'Lint and type checks'};
  document.querySelector('#tQRuns tbody').innerHTML=rk.length?rk.map(r=>
    '<tr><td><b>'+KL[r.kind]+'</b></td><td class="num">'+r.runs+'</td><td class="num">'+r.fails+'</td><td class="num">'+P(r.first_pass_pct)+'</td><td class="num">'+(r.fail_groups?r.recovered_groups+' of '+r.fail_groups:'–')+'</td></tr>').join('')
    :'<tr><td colspan="5" class="empty">No test, build or lint commands detected in this range.</td></tr>';
  mk('cQVerified',{type:'bar',data:{labels:q.weekly.map(w=>fmtD(w.start)),datasets:[{label:'Verified %',data:q.weekly.map(w=>w.pct),backgroundColor:C.emerald}]},
    options:{scales:{x:XGRID,y:{min:0,max:100,...GRID}},plugins:{legend:{display:false},tooltip:{callbacks:{afterBody:i=>{const w=q.weekly[i[0].dataIndex];return w.verified+' of '+w.edited+' edit sessions';}}}}}});
  $('cbHot').innerHTML=cb('hotspots');
  $('qualReFacts').innerHTML='<div><div class="num-lg">'+P(q.reedit_pct)+'</div><div class="fl">of edits are re-edits '+cb('reedit')+'</div></div><div><div class="num-lg">'+q.hotspots.length+'</div><div class="fl">hotspot files</div></div>';
  document.querySelector('#tQHot tbody').innerHTML=q.hotspots.length?q.hotspots.map(h=>
    '<tr><td><b>'+esc(pv(h.file))+'</b><div class="mt" style="font-size:.6875rem;color:var(--txt-3)">'+esc(h.project)+'</div></td><td class="num">'+h.edits+'</td><td class="num">'+h.days+'</td><td class="num">'+h.sessions+'</td></tr>').join('')
    :'<tr><td colspan="4" class="empty">No file was edited over and over in this range.</td></tr>';
  $('qualHotNote').textContent='A hotspot is a file edited 5+ times that was edited 3+ times in one session or on 3+ days. Some files are simply central to your work.';
  $('cbFric').innerHTML=cb('friction');
  $('qualFricFacts').innerHTML=
    '<div><div class="num-lg">'+(q.errors_per_100_edits==null?'–':q.errors_per_100_edits)+'</div><div class="fl">failed tool results per 100 edits</div></div>'+
    '<div><div class="num-lg">'+(q.interrupts_per_100_prompts==null?'–':q.interrupts_per_100_prompts)+'</div><div class="fl">interruptions per 100 prompts</div></div>'+
    '<div><div class="num-lg">'+q.tool_errors+'</div><div class="fl">failed tool results</div></div>'+
    '<div><div class="num-lg">'+q.interruptions+'</div><div class="fl">interruptions</div></div>';
  $('cbSafe').innerHTML=cb('safety');
  const SL={force_push:'Force-pushes',hard_reset:'Hard resets',git_clean:'git clean -f',recursive_delete:'Recursive deletes',pipe_to_shell:'Downloads piped into a shell',skip_hooks:'Skipped git hooks',wide_permissions:'chmod 777',sql_drop:'SQL drops',secret_file_touched:'Secret-like files read or edited'};
  const sk=Object.keys(q.safety);
  $('qualSafety').innerHTML=sk.length?'<ul class="lim">'+sk.map(k=>'<li><b>'+(SL[k]||k)+'</b> ×'+q.safety[k]+'</li>').join('')+'</ul>'
    :'<div class="ad-allgood">'+ic('check').replace('<svg','<svg width="18" height="18" style="color:var(--emerald);flex:none"')+'No risky commands or secret-file touches detected in this range.</div>';
  $('qualPerms').innerHTML=q.permissions.length?q.permissions.map(p=>'<span class="chip2"><b>'+esc(p.decision)+'</b> · '+esc(p.source)+' ×'+p.n+'</span>').join('')
    :'<span class="chip2">Permission decisions are not in your logs.</span>';
}

function renderValue(){
  const v=D.value,pr=(D.prev_insight||{}).value||{};
  const money=x=>'$'+(Math.round(x*100)/100).toFixed(2);
  const tiles=
    tile('Estimated cost',v.ready?money(v.cost):'–','at list prices; $0 on a subscription',C.amber,'cost',insDelta(v.cost,pr.cost,false,money))+
    tile('Per 100 lines',v.per_100_loc==null?'–':money(v.per_100_loc),fmt(v.loc)+' lines written',C.violet,'cost_100_loc',insDelta(v.per_100_loc,pr.per_100_loc,true,money))+
    tile('Per verified session',v.cost_per_verified==null?'–':money(v.cost_per_verified),v.verified_sessions+' edit session'+(v.verified_sessions===1?'':'s')+' ended with a check',C.emerald,'cost_verified',insDelta(v.cost_per_verified,pr.cost_per_verified,true,money))+
    tile('Caching saved',v.ready?money(v.cache_savings):'–',v.cache_saved_pct+'% of what uncached would cost',C.teal,'cache_savings',insDelta(v.cache_savings,pr.cache_savings,false,money));
  const el=$('valHero');
  el.innerHTML='<div class="ins-left"><div><div class="eyebrow">What it costs, and what you get back</div><h2>'+(v.ready?money(v.cost)+' estimated':'No cost data yet')+'</h2><div class="d">'+
    (v.ready?'Median session '+(v.session_median_cost==null?'–':money(v.session_median_cost))+' · '+v.sessions+' sessions. On a subscription plan your real marginal cost is $0; this shows the value you are getting.':'Nothing was recorded in this range.')+'</div></div></div><div class="ins-tiles">'+tiles+'</div>';
  mk('cValDaily',{type:'bar',data:{labels:v.daily.map(x=>x.day.slice(5)),datasets:[{label:'Estimated cost $',data:v.daily.map(x=>x.cost),backgroundColor:v.daily.map(x=>x.spike?C.rose:C.amber)}]},
    options:{scales:{x:XGRID,y:{...GRID,ticks:{padding:8,callback:x=>'$'+Math.round(x*100)/100}}},plugins:{legend:{display:false}}}});
  mk('cValWeekly',{type:'line',data:{labels:v.weekly.map(w=>fmtD(w.start)),datasets:[{label:'$ per 100 lines',data:v.weekly.map(w=>w.per_100_loc),borderColor:C.violet,backgroundColor:areaFill(C.violet),fill:true,spanGaps:true,pointRadius:3}]},
    options:{scales:{x:XGRID,y:{...GRID,ticks:{padding:8,callback:x=>'$'+Math.round(x*100)/100}}},plugins:{legend:{display:false}}}});
  $('cbCache').innerHTML=cb('cache_savings');
  $('valCacheFacts').innerHTML=
    '<div><div class="num-lg">'+(v.ready?money(v.cache_savings):'–')+'</div><div class="fl">saved by caching</div></div>'+
    '<div><div class="num-lg">'+(v.cold_start_pct==null?'–':v.cold_start_pct+'%')+'</div><div class="fl">cold-start tax '+cb('cold_start')+'</div></div>'+
    '<div><div class="num-lg">'+(v.cache_reset_pct==null?'–':v.cache_reset_pct+'%')+'</div><div class="fl">calls that rebuilt the cache</div></div>';
  $('cbMix').innerHTML=cb('model_mix');
  document.querySelector('#tValModels tbody').innerHTML=v.models.length?v.models.map(m=>
    '<tr><td><b>'+esc(m.model)+'</b></td><td class="num">'+money(m.cost)+'</td><td class="num">'+m.share_pct+'%</td><td class="num">'+m.sessions+'</td></tr>').join('')
    :'<tr><td colspan="4" class="empty">No model usage in this range.</td></tr>';
  $('valMixNote').innerHTML=v.top_tier_nocode_pct!=null?'<b>'+v.top_tier_nocode_pct+'%</b> of top-tier-model cost went to sessions where Claude made no edits '+cb('top_tier_nocode')+'.':'';
  $('cbSpike').innerHTML=cb('cost_spikes');
  $('valSpikeCard').style.display=v.spikes.length?'':'none';
  $('valSpikes').innerHTML=v.spikes.map(s=>'<li><b>'+fmtD(s.day)+' · '+money(s.cost)+'</b> ('+(s.x_typical||'?')+'× a typical day)<div class="mt">Biggest session: '+esc(pv(s.session||''))+' ('+money(s.session_cost||0)+')</div></li>').join('');
}

/* Advisor: rule-based recommendations, plus one experiment kept in this browser */
const EXP_KEY='dtd_experiment';
const getExp=()=>{try{return JSON.parse(localStorage.getItem(EXP_KEY)||'null');}catch(e){return null;}};
const putExp=v=>{try{if(v)localStorage.setItem(EXP_KEY,JSON.stringify(v));else localStorage.removeItem(EXP_KEY);}catch(e){}};
const MLABEL={verified_pct:'Edit sessions verified',first_run_pass_pct:'First-run pass rate',clean_pct:'Clean first-pass turns',stuck:'Stuck loops',reedit_pct:'Re-edit share',tests_alongside_pct:'Tests alongside code',recovery_pct:'Failures fixed',cold_start_pct:'Cold-start tax',cache_reset_pct:'Cache rebuilds',per_100_loc:'Cost per 100 lines'};
const AREAC=()=>({quality:C.emerald,flow:C.sky,value:C.amber,habit:C.emerald,range:C.sky,craft:C.violet,impact:C.amber});
function maskQuoted(t){return PRIV?String(t).replace(/"[^"]*"/g,'"•••"'):t;}

function renderAdvisor(){
  const A=D.advisor,ac=AREAC();
  if(!A||!A.ready){$('advRecs').innerHTML='<div class="ins-empty">Not enough activity yet. Keep using Claude Code and this page fills in on its own.</div>';
    $('advWins').innerHTML='';$('advMore').innerHTML='';$('advExp').innerHTML='';return;}
  $('advWindow').textContent=fmtD(A.start)+' – '+fmtD(A.end);
  const exp=getExp();
  $('advRecs').innerHTML=A.recs.length?A.recs.map(r=>{
    const k=ac[r.area]||C.coral,active=exp&&exp.id===r.id;
    return '<div class="ad-step" style="--kc:'+k+';--kbg:'+hexa(k,.13)+'"><div class="meta"><span class="nb">'+r.rank+'</span><span class="cb '+r.confidence+'">'+CONF[r.confidence]+'</span></div>'+
      '<h4>'+esc(r.title)+'</h4><div class="why">'+esc(maskQuoted(r.evidence))+'</div><div class="how">'+esc(r.action)+'</div>'+
      (r.experiment?'<div class="acts"><button class="btn'+(active?'':' accent')+'" data-exp="'+esc(r.id)+'"'+(active?' disabled':'')+'>'+(active?'Experiment running':'Try it as an experiment')+'</button></div>':'')+'</div>';}).join('')
    :'<div class="ad-allgood">'+ic('check').replace('<svg','<svg width="18" height="18" style="color:var(--emerald);flex:none"')+'Nothing stands out. Your data looks healthy in this window.</div>';
  const recById={};A.recs.concat(A.more).forEach(r=>recById[r.id]=r);
  if(exp){
    const cur=A.metrics[exp.metric],base=exp.baseline,better=exp.better;
    const d=(cur!=null&&base!=null)?cur-base:null,good=d==null?null:(better==='down'?d<0:d>0);
    const days=Math.max(0,Math.round((Date.now()-new Date(exp.started).getTime())/864e5));
    $('advExp').innerHTML='<div class="exp-card"><div style="flex:1;min-width:240px"><div class="eyebrow">Running · day '+days+'</div><h4 style="font-size:.9375rem;margin:4px 0 6px">'+esc(exp.label)+'</h4>'+
      '<div class="ad-block" style="color:var(--txt-3);font-weight:500">Tracking: '+esc(MLABEL[exp.metric]||exp.metric)+'. The figure uses a rolling 4-week window, so it moves slowly; give it a week or two.</div></div>'+
      '<div><div class="eyebrow">Before</div><div class="big">'+(base==null?'–':base)+'</div></div><div><div class="eyebrow">Now</div><div class="big">'+(cur==null?'–':cur)+'</div></div>'+
      '<div><div class="eyebrow">Change</div><div class="big '+(good==null?'':good?'up':'down')+'">'+(d==null?'–':(d>0?'+':'')+Math.round(d*10)/10)+'</div></div>'+
      '<button class="btn" id="expStop">Stop experiment</button></div>';
    $('expStop').onclick=()=>{putExp(null);renderAdvisor();};
  }else $('advExp').innerHTML='<div class="ins-empty">No experiment running. Pick <b>Try it as an experiment</b> on any recommendation above. The dashboard records where that number is now, and shows how it moves.</div>';
  document.querySelectorAll('[data-exp]').forEach(b=>b.onclick=()=>{
    const r=recById[b.dataset.exp];if(!r||!r.experiment)return;
    putExp({id:r.id,label:r.experiment.label,metric:r.experiment.metric,better:r.experiment.better,
      baseline:A.metrics[r.experiment.metric],started:new Date().toISOString()});renderAdvisor();});
  $('advWins').innerHTML=A.wins.length?A.wins.map(w=>'<li>'+esc(w)+'</li>').join(''):'<li>Wins show up here as your numbers improve.</li>';
  $('advMore').innerHTML=A.more.length?'<ul class="lim">'+A.more.map(r=>'<li><b>'+esc(r.title)+'</b>: '+esc(maskQuoted(r.evidence))+'</li>').join('')+'</ul>':'<div class="ins-empty">Nothing else of note.</div>';
}

function renderHealth(){
  const H=D.health;
  $('healthCaps').innerHTML=H.capabilities.map(c=>'<div class="it '+c.status+'"><span class="dot"></span><div><b>'+esc(c.label)+'</b><span>'+esc(c.detail)+'</span></div></div>').join('');
  $('healthMeta').innerHTML=
    '<span class="chip2">Sessions <b>'+H.sessions+'</b></span>'+
    (H.versions.length?'<span class="chip2">Claude Code '+H.versions.map(v=>'<b>'+esc(v.version)+'</b>').join(', ')+'</span>':'')+
    (H.entrypoints.length?'<span class="chip2">Via '+H.entrypoints.map(v=>'<b>'+esc(v.name)+'</b>').join(', ')+'</span>':'')+
    '<span class="chip2'+(H.parse_errors?' warn':'')+'">Unparsed lines <b>'+H.parse_errors+'</b></span>'+
    '<span class="chip2">Duplicate lines dropped <b>'+H.duplicate_lines+'</b></span>';
  document.querySelector('#tHealth tbody').innerHTML=H.entry_types.map(t=>'<tr><td><code>'+esc(t.type)+'</code></td><td class="num">'+t.n.toLocaleString()+'</td></tr>').join('');
  $('healthLimits').innerHTML=H.limits.map(l=>'<li>'+esc(l)+'</li>').join('');
  const sel=$('catTab'),tabs=[...new Set(Object.values(D.catalog).map(m=>m.tab))];
  if(sel.options.length===1)tabs.forEach(t=>{const o=document.createElement('option');o.value=t;o.textContent=t.charAt(0).toUpperCase()+t.slice(1);sel.appendChild(o);});
  const f=sel.value;
  document.querySelector('#tCat tbody').innerHTML=Object.values(D.catalog).filter(m=>!f||m.tab===f).map(m=>
    '<tr><td><b>'+esc(m.name)+'</b></td><td>'+esc(m.tab)+'</td><td><span class="cb '+m.confidence+'">'+CONF[m.confidence]+'</span></td><td class="wrapc">'+esc(m.how)+'</td><td class="wrapc">'+esc(m.limit)+'</td></tr>').join('');
  paintPriv();
}
$('catTab').onchange=()=>{if(D)renderHealth();};
$('privBtn').onclick=()=>setPriv(!PRIV);
document.addEventListener('click',e=>{const a=e.target.closest('[data-go]');if(a)setSection(a.dataset.go);});

/* ─────────────────────────── My Adoption ─────────────────────────── */
const AD_COL=()=>({habit:C.emerald,range:C.sky,craft:C.violet,impact:C.amber});
const AD_ICON={habit:'cal',range:'layers',craft:'target',impact:'lever'};
const adDelta=(cur,prev,unit)=>{
  if(prev==null)return'';
  const d=cur-prev;
  if(d===0)return'<span class="flat-d">same as the 4 weeks before</span>';
  return '<span class="'+(d>0?'up':'down')+'">'+(d>0?'▲ ':'▼ ')+Math.abs(d)+(unit||'')+' vs the 4 weeks before</span>';};

function renderAdoption(){
  const A=D.adoption,empty=!A||!A.ready;
  $('adoEmpty').hidden=!empty;$('adoFull').hidden=empty;
  if(empty){
    const n=A?A.active_days:0,need=A?A.min_days:3;
    $('adoEmpty').innerHTML='<div class="chead"><span class="gl"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'+IC.spark+'</svg></span><h3>Building your baseline</h3></div>'+
      '<div style="font-size:.9375rem;color:var(--txt);line-height:1.6;max-width:560px">Your Adoption Score needs at least <b>'+need+' active days</b> in the last 4 weeks to be fair to you. You have <b>'+n+'</b> so far — keep using Claude Code and this page fills in on its own.</div>'+
      '<div class="bar ad-prog" style="max-width:420px"><i style="width:'+Math.min(100,Math.round(n/need*100))+'%"></i></div>'+
      '<div class="note" style="margin-top:0">Nothing is sent anywhere. This is computed from the logs on your machine.</div>';
    return;}
  const col=AD_COL(),L=A.level,pl={};A.pillars.forEach(p=>pl[p.id]=p);
  const R=52,CIRC=2*Math.PI*R,off=CIRC*(1-A.score/100);
  /* ladder: each step fills as the score climbs through it */
  const steps=L.all.map((s,i)=>{
    const hi=i+1<L.all.length?L.all[i+1].floor:100;
    const fill=Math.max(0,Math.min(100,(A.score-s.floor)/(hi-s.floor)*100));
    return '<div class="st'+(i===L.index?' on':'')+'"><i><b style="width:'+Math.round(fill)+'%"></b></i>'+s.name+'</div>';}).join('');
  let chg='';
  if(A.prev_score!=null){const d=A.score-A.prev_score;
    chg='<span class="ad-chg '+(d>0?'up':d<0?'down':'')+'">'+(d>0?'▲ +'+d:d<0?'▼ '+d:'No change')+' vs the 4 weeks before</span>';}
  else chg='<span class="ad-chg">First full window — nothing to compare with yet</span>';
  const nxt=L.next_name?'<b>'+L.to_next+' point'+(L.to_next===1?'':'s')+'</b> to reach '+L.next_name:'You’ve reached the top level — keep it steady';
  const H=A.habit,Rg=A.range,I=A.impact;
  const summary='In the last '+A.window_days+' days you were active on <b>'+H.active_days+'</b> days, used <b>'+Rg.used+' of '+Rg.total+'</b> Claude Code features, and Claude wrote <b>'+fmt(I.loc)+'</b> lines for you. '+
    'Strongest pillar: <b>'+pl[A.strength].label+'</b>. Most room to grow: <b>'+pl[A.growth].label+'</b>.';
  const tiles=A.pillars.map(p=>{
    const c=col[p.id],tag=p.id===A.strength?'<span class="ad-tag">Strongest</span>':(p.id===A.growth?'<span class="ad-tag grow">Room to grow</span>':'');
    return '<div class="ad-pill" style="--kc:'+c+';--kbg:'+hexa(c,.13)+'">'+
      '<div class="khead"><span class="eyebrow">'+p.label+'</span><span class="ic">'+ic(AD_ICON[p.id])+'</span></div>'+
      '<div class="sc"><span class="num-lg">'+p.score+'</span><small>/ 100</small>'+(tag?'<span style="margin-left:auto">'+tag+'</span>':'')+'</div>'+
      '<div class="bar"><i style="width:'+p.score+'%"></i></div>'+
      '<div class="bl">'+p.blurb+'</div>'+
      (p.prev!=null?'<div class="dl">'+adDelta(p.score,p.prev)+'</div>':'')+'</div>';}).join('');
  $('adoHero').innerHTML=
    '<div><div class="ad-top"><div class="ad-ring'+(adoAnim?' anim':'')+'" style="--circ:'+CIRC.toFixed(1)+'">'+
      '<svg viewBox="0 0 120 120"><defs><linearGradient id="adGrad" x1="0" y1="0" x2="1" y2="1">'+
      '<stop offset="0" style="stop-color:var(--acc)"/><stop offset="1" style="stop-color:var(--acc-2)"/></linearGradient></defs>'+
      '<circle class="trk" cx="60" cy="60" r="'+R+'"/>'+
      '<circle class="val" cx="60" cy="60" r="'+R+'" stroke="url(#adGrad)" stroke-dasharray="'+CIRC.toFixed(1)+'" stroke-dashoffset="'+off.toFixed(1)+'"/></svg>'+
      '<div class="mid"><div class="big">'+A.score+'</div><div class="of">out of 100</div></div></div>'+
      '<div class="ad-lvl"><div class="eyebrow">Your level</div><h2>'+L.name+'</h2><div class="d">'+L.desc+'</div>'+chg+'</div></div>'+
      '<div class="ad-ladder">'+steps+'</div><div class="ad-next">'+nxt+'</div>'+(L.blocker?'<div class="ad-block">'+esc(L.blocker)+'</div>':'')+'</div>'+
    '<div class="ad-pills">'+tiles+'</div>'+
    '<div class="ad-sum">'+summary+'</div>';
  adoAnim=false;

  $('adoHl').innerHTML=A.highlights.map(h=>'<li class="'+h.kind+'"><span class="dt"></span><span>'+esc(h.text)+'</span></li>').join('');
  $('adoFunnel').innerHTML=A.funnel.map((x,i)=>'<div class="fs'+(x.achieved?' on':'')+'"><div class="n">'+(x.achieved?'✓':i+1)+'</div><b>'+esc(x.label)+'</b><span>'+esc(x.detail)+'</span></div>').join('');

  /* coach */
  const AREA={habit:'habit',range:'range',craft:'craft',impact:'impact',wellbeing:'habit'};
  $('adoCoach').innerHTML=A.coach.length?A.coach.map((c,i)=>{
    const k=col[AREA[c.area]]||C.coral;
    return '<div class="ad-step" style="--kc:'+k+';--kbg:'+hexa(k,.13)+'"><div class="top"><span class="nb">'+(i+1)+'</span><h4>'+esc(c.title)+'</h4></div>'+
      '<div class="why">'+esc(c.why)+'</div><div class="how">'+esc(c.how)+'</div></div>';}).join('')
    :'<div class="ad-allgood">'+ic('check').replace('<svg','<svg width="18" height="18"')+'You’re covering the bases — nothing to fix right now. Keep going.</div>';

  /* habit */
  $('adoHabitFacts').innerHTML=
    '<div><div class="num-lg">'+H.active_days+'<span style="font-size:.8125rem;color:var(--txt-3);font-weight:520"> / '+A.window_days+'</span></div><div class="fl">active days</div><div class="fd">'+adDelta(H.active_days,H.prev_active_days)+'</div></div>'+
    '<div><div class="num-lg">'+H.weeks_covered+'<span style="font-size:.8125rem;color:var(--txt-3);font-weight:520"> / '+H.weeks+'</span></div><div class="fl">weeks with 2+ active days</div></div>'+
    '<div><div class="num-lg">'+H.streak+'</div><div class="fl">day streak · best '+H.best_streak+'</div></div>';
  const cal=A.calendar,cs=cal.start.split('-').map(Number),mx=[];
  let mh='',ch='';
  for(let c=0;c<17;c++){
    const d0=new Date(cs[0],cs[1]-1,cs[2]+c*7);
    mh+='<span>'+((c===0||d0.getDate()<=7)?MON[d0.getMonth()]:'')+'</span>';}
  cal.cells.forEach((m,i)=>{
    if(m<0){ch+='<div class="ad-c fut"></div>';return;}
    const d=new Date(cs[0],cs[1]-1,cs[2]+i),a=m===0?0:m<40?.24:m<90?.46:m<180?.72:.95;
    ch+='<div class="ad-c" style="'+(a?'background:'+hexa(C.emerald,a):'')+'" title="'+MON[d.getMonth()]+' '+d.getDate()+' · '+(m?fmtMin(m)+' active':'no activity')+'"></div>';});
  $('adoCal').innerHTML='<div class="ad-calm" style="margin-left:32px">'+mh+'</div><div class="ad-calb"><div class="ad-calw"><span>Mon</span><span></span><span>Wed</span><span></span><span>Fri</span><span></span><span></span></div><div class="ad-calg">'+ch+'</div></div>';
  [['adg1',.24],['adg2',.46],['adg3',.72],['adg4',.95]].forEach(x=>{const e=$(x[0]);if(e)e.style.background=hexa(C.emerald,x[1]);});

  mk('cAdoWeeks',{type:'bar',data:{labels:A.weekly.map(w=>fmtD(w.start)),datasets:[
    {label:'Active days',data:A.weekly.map(w=>w.active_days),backgroundColor:C.emerald}]},
    options:{scales:{x:XGRID,y:{min:0,max:7,...GRID,ticks:{stepSize:1,padding:6}}},plugins:{legend:{display:false}}}});

  /* range */
  $('adoRangeHint').textContent=Rg.used+' of '+Rg.total+' in the last '+A.window_days+' days';
  $('adoFeats').innerHTML=Rg.features.map(f=>{
    const on=f.recent>0,lapsed=!on&&f.ever,cls=on?'on':(lapsed?'off lapsed':'off');
    let st;
    if(f.id==='memory')st=on?'CLAUDE.md found in '+f.recent+' active project'+(f.recent===1?'':'s'):'No CLAUDE.md in your active projects';
    else st=on?'Used on '+f.recent+' of the last '+A.window_days+' days':(lapsed?'Used before · not in the last '+A.window_days+' days':'Not tried yet');
    return '<div class="ad-feat '+cls+'"><span class="fi"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">'+(on?IC.check:IC.plus)+'</svg></span>'+
      '<div><b>'+esc(f.label)+'</b><span>'+esc(f.desc)+'</span><em>'+st+'</em>'+(on?'':'<span class="tipl">'+esc(f.tip)+'</span>')+'</div></div>';}).join('');

  /* craft */
  $('adoCraft').innerHTML=A.craft.map(d=>
    '<div class="flrow"><span class="fln">'+d.label+'</span>'+
    '<span class="flb"><span class="bar"><i style="width:'+d.score+'%"></i></span></span>'+
    '<span class="fls">'+d.score+(d.prev!=null&&d.prev!==d.score?'<small class="'+(d.score>d.prev?'up':'down')+'">'+(d.score>d.prev?'▲':'▼')+Math.abs(d.score-d.prev)+'</small>':'')+'</span>'+
    '<span class="flt">'+esc(d.detail)+'<br>'+(d.link?(d.score>=70?'Looking good. ':'')+'<a class="lnk" data-go="'+d.link+'">Open the '+d.label+' tab →</a>':(d.score<70?esc(d.tip):'Looking good.'))+'</span></div>').join('');

  /* impact */
  $('adoImpactFacts').innerHTML=
    '<div><div class="num-lg">'+fmt(I.loc)+'</div><div class="fl">lines Claude wrote</div><div class="fd">'+(I.prev_loc!=null?delta(I.loc,I.prev_loc).replace('vs last period','vs 4 weeks before'):'')+'</div></div>'+
    '<div><div class="num-lg">'+I.code_day_pct+'%</div><div class="fl">of active days shipped code</div></div>'+
    '<div><div class="num-lg">'+I.leverage+'×</div><div class="fl">leverage</div></div>'+
    '<div><div class="num-lg">'+I.verified_pct+'%</div><div class="fl">of code sessions verified</div></div>';
  mk('cAdoLoc',{type:'bar',data:{labels:A.weekly.map(w=>fmtD(w.start)),datasets:[
    {label:'Lines of code',data:A.weekly.map(w=>w.loc),backgroundColor:C.amber}]},
    options:{scales:{x:XGRID,y:GRID},plugins:{legend:{display:false},tooltip:{callbacks:{
      afterBody:i=>{const w=A.weekly[i[0].dataIndex];return w.active_days+' active day'+(w.active_days===1?'':'s')+' · '+w.sessions+' session'+(w.sessions===1?'':'s');}}}}}});

  /* trend */
  const names={};L.all.forEach(x=>{names[x.floor]=x.floor?x.name:'';});
  const stops=L.all.map(x=>x.floor);
  mk('cAdoTrend',{type:'line',data:{labels:A.history.map(h=>fmtD(h.end)),datasets:[{label:'Adoption Score',
    data:A.history.map(h=>h.score),borderColor:C.coral,backgroundColor:areaFill(C.coral),fill:true,
    pointRadius:3,pointBackgroundColor:C.coral,pointBorderColor:C.fill,pointBorderWidth:2}]},
    options:{scales:{x:XGRID,y:{min:0,max:100,...GRID,afterBuildTicks:sc=>{sc.ticks=stops.map(v=>({value:v}));},
      ticks:{padding:8,callback:v=>names[v]!==undefined?names[v]:v}}},plugins:{legend:{display:false}}}});

  /* wellbeing */
  const W=A.wellbeing;
  $('adoWell').innerHTML=
    '<div><div class="num-lg">'+fmtMin(W.avg_active_min)+'</div><div class="fl">active per active day</div></div>'+
    '<div><div class="num-lg">'+fmtMin(W.longest_focus_min)+'</div><div class="fl">longest focus block</div></div>'+
    '<div><div class="num-lg">'+W.late_pct+'%</div><div class="fl">late-night (11pm–6am)</div></div>'+
    '<div><div class="num-lg">'+W.weekend_pct+'%</div><div class="fl">on weekends</div></div>';
  $('adoWellNote').innerHTML=W.late_pct>20?ic('alert').replace('<svg','<svg width="18" height="18" style="color:var(--amber);flex:none"')+'A lot of your activity is late at night. Claude will still be there tomorrow.'
    :(W.weekend_pct>35?ic('alert').replace('<svg','<svg width="18" height="18" style="color:var(--amber);flex:none"')+'A third of your activity lands on weekends. Make sure you’re getting real breaks.'
    :ic('check').replace('<svg','<svg width="18" height="18" style="color:var(--emerald);flex:none"')+'Your rhythm looks sustainable. Steady daytime use beats late-night bursts.');

  /* how it works */
  const wt=A.weights,T=A.targets;
  $('adoHow').innerHTML=[
    ['Habit · '+Math.round(wt.habit*100)+'% of your score','Counts how many days you used Claude Code (full marks at '+T.target_days+' of the last '+A.window_days+', about 3 a week) and how evenly they spread across the 4 weeks.'],
    ['Range · '+Math.round(wt.range*100)+'%','Counts which Claude Code features you actually used (full marks at '+T.features_full+' of '+Rg.total+'), plus how many sessions were substantial ('+T.substantial_actions+'+ tool actions).'],
    ['Craft · '+Math.round(wt.craft*100)+'%','Mostly your four AI-fluency skills (Delegation, Description, Discernment, Diligence, as in the Weekly Report). When there is enough data, 20% each from your Flow and Quality scores are blended in.'],
    ['Impact · '+Math.round(wt.impact*100)+'%','How many lines Claude wrote per active day (full marks at '+T.loc_target+') and on how many of your active days it shipped code.'],
    ['Levels','Explorer 0–19 · Practitioner 20–39 · Power user 40–59 · Expert 60–74 · Pro 75–89 · Pro Max 90+. Pro also needs every pillar at 50+; Pro Max needs every pillar at 70+ and weekly scores of 85+ for 4 weeks running. Scores compare you with your own past, never with other people.'],
    ['Good to know','All heuristics, computed locally from your Claude Code logs. Trends matter more than the exact number. Formulas and thresholds live in docs/METRICS.md and the ADOPTION table in config.py.']
  ].map(x=>'<div class="it"><b>'+x[0]+'</b>'+x[1]+'</div>').join('');
}

/* ─────────────────────────── Weekly report ─────────────────────────── */
function buildPct(w){const t=w.reads+w.writes;return t?Math.round(w.writes/t*100):0;}

function fluencyOf(w){
  const sess=w.sessions,code=sess.filter(s=>s.loc>0);
  const lev=w.human_tokens?w.output/w.human_tokens:0;
  const codeShare=sess.length?code.length/sess.length:0;
  const delegation=Math.round(100*(.5*codeShare+.5*Math.min(1,lev/50)));
  const avgW=w.prompts?(w.prompt_words||0)/w.prompts:0;
  const oneShot=w.prompts?Math.max(0,1-((w.corrections||0)+w.interruptions)/w.prompts):0;
  const sweet=avgW<=0?0:avgW<8?avgW/8:avgW<=150?1:Math.max(0,1-(avgW-150)/300);
  const description=Math.round(100*(.65*oneShot+.35*sweet));
  const engaged=Math.min(1,((w.corrections||0)+w.interruptions)/Math.max(1,w.prompts*.05));
  const errCtl=Math.max(0,1-w.tool_errors/Math.max(1,w.writes||1));
  const discernment=Math.round(100*(.5*engaged+.5*errCtl));
  const verified=code.length?code.filter(s=>s.bash>0).length/code.length:0;
  const diligence=code.length?Math.round(100*verified):0;
  return{delegation,description,discernment,diligence,lev,codeShare,avgW,oneShot,engaged,verified,ncode:code.length};}

function fluencyTips(f){return{
  delegation:f.delegation<50?'Try handing Claude whole tasks (implement + verify), not just questions.':'Healthy mix of asking and delegating real work.',
  description:f.description<50?'Front-load constraints, file paths and expected behavior in the first prompt.':'Most prompts land without needing a correction.',
  discernment:f.engaged<.3?'You rarely push back — spot-check diffs and say so when output misses.':(f.discernment<50?'High error rate — slow down and review output before continuing.':'You actively steer and catch issues.'),
  diligence:f.ncode===0?'No code sessions this week.':(f.diligence<50?'Ask Claude to run tests/commands after edits — most code sessions never verified.':'Most code sessions verified their work with commands.')};}

function renderFluency(w,prev){
  const f=fluencyOf(w),tips=fluencyTips(f),fp=prev?fluencyOf(prev):null;
  const dims=[
    ['Delegation',f.delegation,'<b>'+Math.round(f.codeShare*100)+'%</b> of sessions shipped code · <b>'+f.lev.toFixed(1)+'×</b> leverage'],
    ['Description',f.description,'<b>'+Math.round(f.oneShot*100)+'%</b> of prompts needed no correction · <b>'+Math.round(f.avgW)+'</b> words avg'],
    ['Discernment',f.discernment,'<b>'+(w.corrections||0)+'</b> corrections + <b>'+w.interruptions+'</b> interruptions · <b>'+w.tool_errors+'</b> tool errors'],
    ['Diligence',f.diligence,'<b>'+Math.round(f.verified*100)+'%</b> of '+f.ncode+' code session'+(f.ncode===1?'':'s')+' ran commands after edits']];
  const key=['delegation','description','discernment','diligence'];
  $('flList').innerHTML=dims.map((d,i)=>
    '<div class="flrow"><span class="fln">'+d[0]+'</span>'+
    '<span class="flb"><span class="bar"><i style="width:'+d[1]+'%"></i></span></span>'+
    '<span class="fls">'+d[1]+'</span>'+
    '<span class="flt">'+d[2]+'<br>'+tips[key[i]]+'</span></div>').join('');
  const ds=[{label:'This week',data:dims.map(d=>d[1]),borderColor:C.coral,
    backgroundColor:hexa(C.coral,.17),pointBackgroundColor:C.coral,borderWidth:2}];
  if(fp)ds.push({label:'Last week',data:[fp.delegation,fp.description,fp.discernment,fp.diligence],
    borderColor:C.sky,backgroundColor:hexa(C.sky,.07),pointBackgroundColor:C.sky,borderWidth:1.5});
  mk('cFluency',{type:'radar',data:{labels:dims.map(d=>d[0]),datasets:ds},
    options:{scales:{r:{min:0,max:100,ticks:{display:false,stepSize:25},
      grid:{color:C.grid},angleLines:{color:C.grid},
      pointLabels:{color:C.txt2,font:{size:11.5,weight:'600'}}}},
      plugins:{legend:{position:'bottom'}}}});}

let reflQtext='',reflWeekKey=null;
function pickReflection(w,idx){
  const c=[];
  const corrRate=w.prompts?(w.corrections||0)/w.prompts:0;
  if(corrRate>0.08)c.push('You corrected Claude '+w.corrections+' times this week ('+Math.round(corrRate*100)+'% of prompts). What context could your first prompts include so the second try isn’t needed?');
  const late=w.messages?(w.late_msgs||0)/w.messages:0;
  if(late>0.2)c.push(Math.round(late*100)+'% of this week’s activity happened between 11pm and 6am. Is late-night coding a deliberate choice — or a habit worth questioning?');
  const tot=w.reads+w.writes,ex=tot?w.reads/tot:0;
  if(tot&&ex>0.65)c.push(Math.round(ex*100)+'% of tool calls this week were exploration. Are you using Claude mostly to understand code — and is there building you could delegate too?');
  if(tot&&ex<0.15)c.push(Math.round((1-ex)*100)+'% of tool calls this week were edits. Are you reading and verifying what gets written — or shipping on trust?');
  const code=w.sessions.filter(s=>s.loc>0),ver=code.length?code.filter(s=>s.bash>0).length/code.length:1;
  if(code.length>2&&ver<0.4)c.push('Only '+Math.round(ver*100)+'% of code-writing sessions ran a command afterwards. How do you know this week’s '+fmt(w.loc)+' new lines actually work?');
  c.push('What’s one thing you want to keep doing yourself, even if Claude could do it faster?');
  return c[idx%c.length];}

function renderWeek(){
  const wk=D.weeks;
  if(!wk.length){$('secSub').textContent='No data yet.';
    $('wKpis').innerHTML='<div class="empty">No weeks recorded yet.</div>';return;}
  if(wIdx===null)wIdx=wk.length-1;
  const w=wk[wIdx],prev=wIdx>0?wk[wIdx-1]:null;
  $('wPrev').disabled=wIdx===0;$('wNext').disabled=wIdx===wk.length-1;
  updateChrome();
  const bp=buildPct(w);
  const kpis=[
    ['Sessions',w.sessions.length,delta(w.sessions.length,prev&&prev.sessions.length),C.sky],
    ['Focus hours',(w.minutes/60).toFixed(1),delta(w.minutes,prev&&prev.minutes),C.teal],
    ['Output tokens',fmt(w.output),delta(w.output,prev&&prev.output),C.emerald],
    ['Lines of code',fmt(w.loc),delta(w.loc,prev&&prev.loc),C.violet],
    ['Prompts',fmt(w.prompts),delta(w.prompts,prev&&prev.prompts),C.coral],
    ['Building',bp+'%',(100-bp)+'% exploration',C.amber]];
  $('wKpis').innerHTML=kpis.map(k=>
    '<div class="kpi plain" style="--kc:'+k[3]+'"><div class="khead"><span class="eyebrow">'+k[0]+
    '</span></div><div class="num-lg">'+k[1]+'</div><div class="s">'+(k[2]||'')+'</div></div>').join('');
  const g={};w.sessions.forEach(s=>{(g[s.project]=g[s.project]||[]).push(s)});
  const keys=Object.keys(g);
  $('wWork').innerHTML=keys.length?keys.map(p=>
    '<div class="grp"><h4>'+esc(p)+'</h4><ul>'+g[p].map(s=>
      '<li>'+esc(pv(s.title))+'<div class="meta">'+fmtD(s.day)+' · '+s.min+' min · '+
      fmt(s.loc)+' LoC · '+s.prompts+' prompts</div></li>').join('')+'</ul></div>').join('')
    :'<div class="empty">No sessions logged this week.</div>';
  mk('cSplit',{type:'doughnut',data:{labels:['Building (file edits)','Exploration (reads/searches)'],
    datasets:[{data:[w.writes,w.reads],backgroundColor:[C.emerald,C.sky],
      borderColor:cssv('--fill'),borderWidth:3,hoverOffset:8}]},
    options:{cutout:'68%',plugins:{legend:{position:'bottom'}}}});
  mk('cWeekDays',{type:'bar',data:{labels:w.days.map(x=>fmtD(x.day)),datasets:[
    {label:'Output tokens',data:w.days.map(x=>x.output),backgroundColor:C.sky,yAxisID:'y'},
    {label:'LoC',data:w.days.map(x=>x.loc),backgroundColor:C.violet,yAxisID:'y1'}]},
    options:{scales:{x:XGRID,y:{position:'left',...GRID},
      y1:{position:'right',grid:{drawOnChartArea:false},border:{display:false}}},
      plugins:{legend:{position:'bottom'}}}});
  renderFluency(w,prev);
  reflQtext=pickReflection(w,wIdx);
  $('reflQ').textContent=reflQtext;
  /* clear the discussion only when switching weeks — not on the 15s refresh */
  if(reflWeekKey!==w.key){reflWeekKey=w.key;$('reflOut').textContent='';}
}
$('wPrev').onclick=()=>{if(wIdx>0){wIdx--;renderWeek();}};
$('wNext').onclick=()=>{if(D&&wIdx<D.weeks.length-1){wIdx++;renderWeek();}};

$('reflBtn').onclick=async()=>{
  if(!D||!D.weeks.length||!reflQtext)return;
  const btn=$('reflBtn'),out=$('reflOut'),old=btn.innerHTML;
  btn.disabled=true;btn.textContent='Thinking…';
  out.textContent='Running claude -p — this can take a minute…';
  try{const r=await fetch('/api/reflect?week='+encodeURIComponent(D.weeks[wIdx].key)+
    '&q='+encodeURIComponent(reflQtext));
    const j=await r.json();out.textContent=j.ok?j.text:('Failed: '+j.error);}
  catch(e){out.textContent='Failed: '+e;}
  btn.disabled=false;btn.innerHTML=old;};

function weekMarkdown(w){
  const g={};w.sessions.forEach(s=>{(g[s.project]=g[s.project]||[]).push(s)});
  let md='# Weekly Update — '+fmtD(w.start)+' to '+fmtD(w.end)+'\n\n## What I worked on\n';
  for(const p in g){md+='\n### '+p+'\n';g[p].forEach(s=>{md+='- '+s.title+' ('+s.day+', ~'+s.min+' min)\n';});}
  const bp=buildPct(w);
  md+='\n## Numbers\n';
  md+='- '+w.sessions.length+' sessions, ~'+(w.minutes/60).toFixed(1)+' focus-hours\n';
  md+='- '+w.loc.toLocaleString()+' lines of code written with Claude\n';
  md+='- '+w.prompts.toLocaleString()+' prompts · '+w.output.toLocaleString()+' tokens generated\n';
  md+='- Work split: '+bp+'% building / '+(100-bp)+'% exploration\n';
  const tc={};w.sessions.forEach(s=>{const t=s.type||'other';tc[t]=(tc[t]||0)+1});
  const mix=TT.filter(t=>tc[t]).map(t=>tc[t]+' '+t).join(', ');
  if(mix)md+='- Task mix: '+mix+'\n';
  return md;}

async function copyText(txt){
  try{await navigator.clipboard.writeText(txt);}
  catch(e){const ta=document.createElement('textarea');ta.value=txt;
    document.body.appendChild(ta);ta.select();document.execCommand('copy');ta.remove();}}
function flash(btn,label){const old=btn.innerHTML;btn.textContent=label;
  setTimeout(()=>{btn.innerHTML=old;},1600);}

$('wCopy').onclick=async()=>{if(!D||!D.weeks.length)return;
  await copyText(weekMarkdown(D.weeks[wIdx]));flash($('wCopy'),'✓ Copied');};

$('wAI').onclick=async()=>{
  if(!D||!D.weeks.length)return;
  const btn=$('wAI'),card=$('wAIcard'),out=$('wAItext'),old=btn.innerHTML;
  btn.disabled=true;btn.textContent='Generating…';card.style.display='';
  out.textContent='Running claude -p — this can take a minute…';
  try{const r=await fetch('/api/ai_summary?week='+encodeURIComponent(D.weeks[wIdx].key));
    const j=await r.json();out.textContent=j.ok?j.text:('Failed: '+j.error);}
  catch(e){out.textContent='Failed: '+e;}
  btn.disabled=false;btn.innerHTML=old;};

/* ═══════════════════ Project sheet: grabbable, velocity-aware ═══════════════════
   Open/close is a spring on `openP`; the drag offset is a second, independent
   spring. Dragging jumps the offset 1:1 with the finger (cancelling any
   in-flight animation), release projects the momentum forward and either
   dismisses or springs home carrying the release velocity. */
const sheet=$('sheet'),scrim=$('scrim'),sbox=$('sBox');
let sheetOn=false;
function paintSheet(){
  const p=openP.v,y=dragY.v;
  sbox.style.transform='translate3d(0,'+(y+(1-p)*22).toFixed(2)+'px,0) scale('+(.965+.035*p).toFixed(4)+')';
  sbox.style.opacity=Math.max(0,Math.min(1,p)).toFixed(3);
  sbox.style.filter=p<.999?'blur('+((1-p)*7).toFixed(2)+'px)':'none';
  scrim.style.opacity=(Math.max(0,Math.min(1,p))*Math.max(0,1-Math.max(0,y)/420)).toFixed(3);
}
const openP=new Spring(0,paintSheet,{damping:1,response:.36});
const dragY=new Spring(0,paintSheet,{damping:.8,response:.34});
openP.done=()=>{if(openP.target===0&&!sheetOn){sheet.style.display='none';scrim.style.display='none';}};

function openSheet(){sheetOn=true;sheet.style.display='flex';scrim.style.display='block';
  sheet.scrollTop=0;dragY.jump(0);paintSheet();openP.to(1);
  setTimeout(()=>$('sClose').focus(),60);}
function closeSheet(vel){
  if(!sheetOn)return;sheetOn=false;
  openP.to(0);dragY.to(Math.max(260,innerHeight*.45),vel);}
$('sClose').onclick=()=>closeSheet();
scrim.onclick=()=>closeSheet();
sheet.addEventListener('click',e=>{if(e.target===sheet)closeSheet();});
addEventListener('keydown',e=>{if(e.key==='Escape'){if(popOpen)togglePop(false);else if(sheetOn)closeSheet();}});

/* 1:1 tracking with grab offset, rubber-banding upward, momentum on release */
(function(){
  const grab=$('sGrab');let pid=null,startY=0,base=0,hist=[];
  grab.addEventListener('pointerdown',e=>{
    if(e.target.closest('#sClose'))return;
    pid=e.pointerId;grab.setPointerCapture(pid);
    startY=e.clientY;base=dragY.v;hist=[[e.clientY,performance.now()]];});
  grab.addEventListener('pointermove',e=>{
    if(pid===null||e.pointerId!==pid)return;
    let dy=base+(e.clientY-startY);
    if(dy<0)dy=-rubber(-dy,innerHeight);          // soft boundary, never a hard stop
    dragY.jump(dy);                                // continuous feedback, not on release
    hist.push([e.clientY,performance.now()]);if(hist.length>6)hist.shift();});
  const end=e=>{
    if(pid===null||(e&&e.pointerId!==pid))return;
    try{grab.releasePointerCapture(pid);}catch(err){}
    pid=null;
    let v=0;
    if(hist.length>1){const a=hist[0],b=hist[hist.length-1],dt=(b[1]-a[1])/1000;
      if(dt>0.001)v=(b[0]-a[0])/dt;}
    const projected=dragY.v+projectTo(v);          // where the flick is going
    if(projected>170||v>900)closeSheet(v);
    else dragY.to(0,v);};                          // hand the velocity to the spring
  grab.addEventListener('pointerup',end);
  grab.addEventListener('pointercancel',end);
})();

function openProject(p){
  $('sTitle').textContent=p.name.split('/').pop()||p.name;
  $('sSub').innerHTML=esc(p.name)+' · '+fmt(p.input)+' in · '+fmt(p.output)+' out · '+
    fmt(p.loc)+' LoC · '+p.sessions+' sessions · est. $'+p.cost;
  openSheet();
  mk('cProj',{type:'bar',data:{labels:p.days.map(x=>x.day.slice(5)),datasets:[
    {label:'Output tokens',data:p.days.map(x=>x.output),backgroundColor:C.sky,yAxisID:'y'},
    {label:'LoC',data:p.days.map(x=>x.loc),backgroundColor:C.violet,yAxisID:'y1'}]},
    options:{scales:{x:XGRID,y:{position:'left',...GRID},
      y1:{position:'right',grid:{drawOnChartArea:false},border:{display:false}}},
      plugins:{legend:{position:'bottom'}}}});
  const sess=(D.sessions_list||[]).filter(s=>s.project===p.name);
  $('sSess').innerHTML=sess.length?sess.map(s=>
    '<li><b>'+esc(pv(s.title))+'</b><div class="mt">'+s.day+' · '+s.min+' min · '+fmt(s.loc)+
    ' LoC · '+s.prompts+' prompts · '+fmt(s.output)+' tokens out</div></li>').join('')
    :'<li class="empty">No session details available.</li>';
  document.querySelector('#sFiles tbody').innerHTML=(p.files||[]).length?(p.files||[]).map(f=>
    '<tr><td>'+esc(pv(f[0]))+'</td><td class="num">'+fmt(f[1])+'</td></tr>').join('')
    :'<tr><td colspan="2" class="empty">No files recorded.</td></tr>';
}

/* ─────────────────────────── Exports ─────────────────────────── */
function download(name,text,type){
  const b=new Blob([text],{type}),u=URL.createObjectURL(b);
  const a=document.createElement('a');a.href=u;a.download=name;
  document.body.appendChild(a);a.click();a.remove();URL.revokeObjectURL(u);}
function rangeLabel(){return (customFrom||customTo)
  ?((customFrom||'start')+'_to_'+(customTo||'now'))
  :(days?('last'+days+'d'):'alltime');}
$('expJson').onclick=()=>{if(D)download('claude-usage_'+rangeLabel()+'.json',
  JSON.stringify(D,null,2),'application/json');};
$('expCsv').onclick=()=>{if(!D)return;
  const rows=[['date','input','output','cache_read','cache_write','loc','prompts','reads','writes','est_cost_usd']];
  D.days.forEach(x=>rows.push([x.day,x.input,x.output,x.cache_read,x.cache_write,
    x.loc,x.prompts,x.reads,x.writes,x.cost]));
  download('claude-usage_'+rangeLabel()+'.csv',rows.map(r=>r.join(',')).join('\n'),'text/csv');};
$('expCopy').onclick=async()=>{if(!D)return;const t=D.totals;
  await copyText('Claude Code usage — '+rangeLabel().replace(/_/g,' ')+'\n'+
    'Output tokens: '+t.output.toLocaleString()+'\nInput tokens: '+t.input.toLocaleString()+'\n'+
    'Cache read: '+t.cache_read.toLocaleString()+' ('+t.cache_efficiency+'% hit)\n'+
    'Lines of code: '+t.loc.toLocaleString()+'\nPrompts: '+t.prompts.toLocaleString()+'\n'+
    'Sessions: '+t.sessions+' ('+t.avg_session_min+' min avg)\n'+
    'Est. API cost: $'+t.cost+'\nLeverage: '+t.leverage+'x');
  flash($('expCopy'),'✓');};

/* ─────────────────────────── Boot ─────────────────────────── */
/* Chart.js has a rare first-paint race in its animator (creating several
   charts in one tick can throw asynchronously inside its own rAF loop,
   "this._fn is not a function"). It's not catchable at the call site since
   it happens after mk() returns — but a plain re-render always recovers,
   so treat it as a one-shot self-heal rather than a fatal error. */
let chartRecoverAt=0;
addEventListener('error',e=>{
  if(!/chart\.umd\.min\.js/.test(e.filename||''))return;
  const now=Date.now();
  if(now-chartRecoverAt<1000)return;   // avoid a retry storm if it keeps failing
  chartRecoverAt=now;
  if(D)render();
});
applyTheme(false);
updateChrome();
load();
setInterval(load,15000);

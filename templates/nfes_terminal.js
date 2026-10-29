// NFES 交易終端前端（/terminal/nfes），資料見 portfolio_app.py 的 _build_nfes_terminal；與 SAM 版各自獨立
const $=id=>document.getElementById(id);
const BOT='nfes';
const API='/api/nfes/terminal';
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const usd=n=>n==null?'—':'$'+(+n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const sgn=(n,d=2)=>(n>=0?'+':'−')+Math.abs(n).toFixed(d);
const tsd=s=>{const d=new Date(s);return isNaN(d)?'':`${d.getMonth()+1}/${d.getDate()} ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`};
const hm=s=>{const d=new Date(s);return isNaN(d)?'':`${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`};
const W=ms=>new Promise(r=>setTimeout(r,ms));

/* ── agents（第一次拿到資料才建立）── */
let built=false;
function buildAgents(list){
 list.forEach(a=>{const d=document.createElement('div');d.className='ag';d.id='ag-'+a.k;d.style.setProperty('--k',`var(${a.c})`);
  d.innerHTML=`<div class="stamp">FILTERED</div><div class="ag-h"><div class="ic">${esc(a.ic)}</div><div><div class="nm">${esc(a.nm)}</div><div class="ds">${esc(a.ds)}</div></div><span class="pill">${esc(a.pill)}</span></div><div class="rule">${esc(a.rule)}</div><div class="now">—</div>`;
  $('agents').appendChild(d)});
 const risk=list.find(a=>a.k==='risk');if(risk)RK.label.textContent=risk.nm;
 built=true}
function act(k,ms=1500){const el=$('ag-'+k);if(!el)return;el.classList.add('on');clearTimeout(el._t);el._t=setTimeout(()=>el.classList.remove('on'),ms)}

/* ── flow svg ── */
const NS="http://www.w3.org/2000/svg",S=$('fsvg');
const svg=(t,a,p=S)=>{const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);p.appendChild(e);return e};
const CH=[{x:14,c:"#5b8cff"},{x:162,c:"#a78bfa"},{x:310,c:"#f5c84c"},{x:458,c:"#ff9f43"}];
const RN={x:170,y:160},EN={x:470,y:160};
const chipPath=i=>`M${CH[i].x+64},62 C${CH[i].x+64},110 ${RN.x},110 ${RN.x},${RN.y-30}`;
CH.forEach((c,i)=>svg('path',{d:chipPath(i),fill:"none",stroke:"#2a3344","stroke-width":1.2,class:"march"}));
svg('path',{d:`M${RN.x+30},${RN.y} L${EN.x-30},${EN.y}`,fill:"none",stroke:"#2a3344","stroke-width":1.2,class:"march"});
svg('text',{x:320,y:RN.y+20,"text-anchor":"middle",fill:"#7c8597","font-size":10,"font-family":"JetBrains Mono"}).textContent="通過 → 倉位 → 進場";
const chips=CH.map(c=>{const g=svg('g',{});
 const r=svg('rect',{x:c.x,y:12,width:128,height:50,rx:8,fill:"#10151d",stroke:c.c,"stroke-width":1.2},g);
 const t=svg('text',{x:c.x+12,y:33,fill:"#e8ebf1","font-size":13,"font-weight":700,"font-family":"JetBrains Mono"},g);
 const s=svg('text',{x:c.x+12,y:50,fill:"#7c8597","font-size":10,"font-family":"JetBrains Mono"},g);
 svg('circle',{cx:c.x+116,cy:24,r:3,fill:c.c},g);return{r,t,s,c:c.c}});
function node(p,col,label,sub){svg('circle',{cx:p.x,cy:p.y,r:38,fill:"none",stroke:col,"stroke-opacity":.18,"stroke-width":8});
 svg('circle',{cx:p.x,cy:p.y,r:30,fill:"#0c1016",stroke:col,"stroke-width":1.6,class:"node-ring","stroke-dasharray":"150 40"});
 const core=svg('circle',{cx:p.x,cy:p.y,r:17,fill:col,"fill-opacity":.18});
 svg('text',{x:p.x,y:p.y+5,"text-anchor":"middle",fill:col,"font-size":15}).textContent=label;
 const lb=svg('text',{x:p.x,y:p.y+54,"text-anchor":"middle",fill:col,"font-size":9.5,"font-weight":700,"font-family":"JetBrains Mono","letter-spacing":".1em"});lb.textContent=sub;
 const st=svg('text',{x:p.x,y:p.y+66,"text-anchor":"middle",fill:"#7c8597","font-size":9.5,"font-family":"JetBrains Mono"});st.textContent='待命';return{core,st,label:lb}}
const RK=node(RN,"#ff4d5e","▲","FILTER"),EX=node(EN,"#38d6e8","▶","EXECUTOR");
const pt=svg('circle',{r:5,fill:"#fff",opacity:0,style:"filter:drop-shadow(0 0 6px #fff)"});
const tmpP=svg('path',{fill:"none",stroke:"none"});
function travel(d,col,ms){return new Promise(res=>{tmpP.setAttribute('d',d);const L=tmpP.getTotalLength(),t0=performance.now();pt.setAttribute('fill',col);pt.setAttribute('opacity',1);
 (function f(t){const k=Math.min(1,(t-t0)/ms),e=k<.5?2*k*k:1-Math.pow(-2*k+2,2)/2,p=tmpP.getPointAtLength(L*e);pt.setAttribute('cx',p.x);pt.setAttribute('cy',p.y);k<1?requestAnimationFrame(f):(pt.setAttribute('opacity',0),res())})(t0)})}
function flash(n){n.core.animate([{fillOpacity:.75},{fillOpacity:.18}],{duration:700})}

/* ── data ── */
let D=null,shownPnl=null;
async function load(){
 try{const r=await fetch(API,{cache:'no-store'});
  if(r.status===401){location.reload();return}
  if(!r.ok)throw new Error('HTTP '+r.status);D=await r.json();render();}
 catch(e){const l=$('live');l.style.color='var(--r)';l.querySelector('span').textContent='讀取失敗：'+e.message}
}

function render(){
 const M=D.meta,now=new Date(D.now),eq=D.equity,m0=D.month_start;
 if(!built)buildAgents(D.agents);
 document.title=`${M.name} 交易終端`;$('bname').textContent=M.name;$('logo').textContent=M.logo;$('ver').textContent=M.version;
 $('logname').textContent='$ tail -f '+M.log_name;$('foot').textContent=`資料來源：${M.src}・每 30 秒更新`+(D.synced_at?`・同步於 ${tsd(D.synced_at)}`:'');
 const age=(Date.now()-new Date(D.last_scan))/60000,l=$('live'),ok=age<M.stall_min;
 l.style.color=ok?'var(--g)':'var(--r)';l.querySelector('span').textContent=ok?`LIVE · 最後掃描 ${hm(D.last_scan)}`:`掃描停滯 ${Math.round(age)} 分鐘`;
 $('online').textContent=ok?`${D.agents.length} online`:'scanner stalled';

 const pnl=eq-m0,pc=pnl/m0*100;shownPnl??=pnl;
 $('hero').style.setProperty('--glow',pnl>=0?'rgba(52,211,153,.22)':'rgba(255,77,94,.20)');
 $('subv').innerHTML=`<span class="${pc>=0?'up':'dn'}">${sgn(pc)}%</span> <span style="color:var(--dim)">· 月初 ${usd(m0)} → ${usd(eq)}</span>`;
 $('goalt').hidden=M.goal_pct==null;if(M.goal_pct!=null)$('goalv').textContent=`+${M.goal_pct}%`;
 const mw=D.month.wins,ml=D.month.losses;$('wins').textContent=mw;$('losses').textContent=ml;
 $('wr').textContent=`本月 ${mw+ml} 筆 · 勝率 ${mw+ml?Math.round(mw/(mw+ml)*100)+'%':'—'}`;
 $('dots').innerHTML=D.dots.map(w=>`<i class="${w?'w':'l'}"></i>`).join('');

 $('skips').textContent=D.today.filtered;$('t1').textContent=D.today.filtered;$('t1s').textContent=D.today.label;
 $('t2').textContent=D.today.approved;$('t3').textContent=D.today.checked;$('t4').textContent=usd(eq);$('t4s').textContent=D.eq_sub;

 const sc=v=>Math.max(3,Math.min(100,50+(v/m0-1)*250));
 $('m0v').textContent=usd(m0);$('nowv').textContent=usd(eq);$('nowv').className=eq>=m0?'up':'dn';
 $('nowbar').style.width=sc(eq)+'%';$('nowbar').style.background=eq>=m0?'linear-gradient(90deg,var(--c),var(--g))':'linear-gradient(90deg,#7a2a35,var(--r))';
 $('rng').textContent=`${now.getMonth()+1}/1 → ${now.getMonth()+1}/${now.getDate()}`;

 D.agents.forEach(a=>{const el=$('ag-'+a.k);if(!el)return;el.querySelector('.now').textContent=a.now;el.querySelector('.pill').textContent=a.pill});

 const cs=D.cards||[],op=cs.length&&cs[0].open;
 $('tcap').textContent=op?`TRADE · 持倉中 ×${cs.length}`:'TRADE · 最近一筆';
 $('tst').textContent=cs.length?(op?'OPEN':'CLOSED'):'—';$('tst').style.color=op?'var(--g)':'var(--dim)';
 $('tcards').innerHTML=cs.map(t=>`<div class="tcard">
  <div class="sym"><b>$${esc(t.symbol)}</b>${t.side?`<span class="pill" style="color:${t.side==='short'?'var(--r)':'var(--g)'}">${esc(t.side.toUpperCase())}</span>`:''}<span class="pnl ${(t.pnl||0)>=0?'up':'dn'}">${esc(t.pnl_text)}</span></div>
  <div class="kv"><span>進場</span><span>${esc(t.entry)}</span><span>出場</span><span>${esc(t.exit)}</span><span>倉位</span><span>${esc(t.size)}</span><span>結果</span><span>${esc(t.result||'—')}</span></div>
  ${t.reason?`<div class="reason">理由：${esc(t.reason)}</div>`:''}</div>`).join('');

 const tl=$('tail'),key=D.log.map(x=>x.t+x.msg).join('|');
 if(tl._key!==key){const prev=new Set((tl._key||'').split('|'));tl._key=key;
  const col={SCAN:'var(--y)',PERF:'var(--b)',WATCH_CHECK:'var(--o)',BTC_REGIME:'var(--p)',RSI_FILTER:'var(--r)',MCAP:'var(--p)',SKIP:'var(--o)',TICKER:'var(--mute)'};
  tl.innerHTML=D.log.slice(-16).map(x=>`<div class="l${prev.has(x.t+x.msg)?'':' new'}"><span style="color:var(--mute)">${x.t}</span> <span style="color:${col[x.tag]||(x.lvl==='WARNING'||x.lvl==='ERROR'?'var(--r)':'var(--g)')}">${esc(x.tag)}</span> ${esc(x.msg)}</div>`).join('')+'<div class="l"><span class="cur"></span></div>';
  $('tn').textContent=`${D.log.length} lines`}

 const TC={WIN:'var(--g)',LOSS:'var(--r)',BE:'var(--dim)',FILTERED:'var(--r)',PASS:'var(--c)'};
 $('lt').innerHTML=D.rows.map(r=>{const rt=r.pnl!=null?`<span class="${r.pnl>=0?'up':'dn'}">${sgn(r.pnl)}U</span>`:r.tag==='PASS'?`<span style="color:var(--c)">${esc(r.side||'')}</span>`:'<span style="color:var(--dim)">不交易</span>';
  return `<div class="lr"><span class="tm" style="color:var(--mute)">${tsd(r.ts)}</span><span class="pill" style="color:${TC[r.tag]||'var(--dim)'}">${r.tag}</span><b>${r.symbol?'$'+esc(r.symbol):''}</b><span class="rt">${rt}</span><span class="wy" title="${esc(r.why)}">${esc(r.why)}</span></div>`}).join('');
}

/* ── 主卡數字與曲線 ── */
const cv=$('spark'),cx=cv.getContext('2d');
function frame(){
 if(D){const pnl=D.equity-D.month_start;shownPnl+=(pnl-shownPnl)*.08;
  $('big').textContent=(shownPnl>=0?'+':'−')+'$'+Math.abs(shownPnl).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
  $('big').style.color=pnl>=0?'var(--fg)':'var(--r)';$('big').style.textShadow=pnl>=0?'0 0 30px rgba(52,211,153,.35)':'0 0 30px rgba(255,77,94,.35)';drawSpark()}
 requestAnimationFrame(frame)}
function drawSpark(){const r=devicePixelRatio||1,w=cv.clientWidth,h=cv.clientHeight;if(cv.width!==Math.round(w*r)){cv.width=w*r;cv.height=h*r}cx.setTransform(r,0,0,r,0,0);cx.clearRect(0,0,w,h);
 const n=new Date(D.now),ms=new Date(n.getFullYear(),n.getMonth(),1),m0=D.month_start,goal=D.meta.goal_pct!=null?m0*(1+D.meta.goal_pct/100):null;
 const a=[m0,...D.curve.filter(x=>new Date(x.t)>=ms).map(x=>x.eq),D.equity];
 const ext=goal!=null?[goal]:[],mn=Math.min(...a)*.995,mx=Math.max(...a,...ext)*1.005,y=v=>h-6-(v-mn)/(mx-mn)*(h-20),sx=(w-8)/(a.length-1);
 cx.font='10px "JetBrains Mono",monospace';cx.setLineDash([3,4]);
 if(goal!=null){cx.strokeStyle='rgba(245,200,76,.5)';cx.beginPath();cx.moveTo(0,y(goal));cx.lineTo(w,y(goal));cx.stroke();cx.fillStyle='rgba(245,200,76,.8)';cx.fillText(`月目標 +${D.meta.goal_pct}%`,4,y(goal)-3)}
 cx.strokeStyle='rgba(124,133,151,.35)';cx.beginPath();cx.moveTo(0,y(m0));cx.lineTo(w,y(m0));cx.stroke();cx.setLineDash([]);
 cx.fillStyle='rgba(124,133,151,.8)';cx.fillText('月初',4,y(m0)+11);
 const up=a[a.length-1]>=m0,C=up?'52,211,153':'255,77,94';
 const g=cx.createLinearGradient(0,0,0,h);g.addColorStop(0,`rgba(${C},.28)`);g.addColorStop(1,`rgba(${C},0)`);
 cx.beginPath();a.forEach((v,i)=>i?cx.lineTo(i*sx,y(v)):cx.moveTo(0,y(v)));cx.lineTo((a.length-1)*sx,h);cx.lineTo(0,h);cx.fillStyle=g;cx.fill();
 cx.beginPath();a.forEach((v,i)=>i?cx.lineTo(i*sx,y(v)):cx.moveTo(0,y(v)));cx.strokeStyle=`rgb(${C})`;cx.lineWidth=2;cx.shadowColor=`rgb(${C})`;cx.shadowBlur=10;cx.stroke();cx.shadowBlur=0;
 const ex=(a.length-1)*sx,ey=y(a[a.length-1]),k=(Date.now()/700)%1;cx.fillStyle=`rgb(${C})`;cx.beginPath();cx.arc(ex,ey,3.5,0,7);cx.fill();cx.strokeStyle=`rgba(${C},${1-k})`;cx.beginPath();cx.arc(ex,ey,3+k*10,0,7);cx.stroke()}
requestAnimationFrame(frame);

/* ── 重播真實決策 ── */
async function replay(){
 let i=0;
 while(true){
  if(!D||!D.decisions.length){await W(1000);continue}
  const list=D.decisions,e=list[i%list.length],slot=i%4,ch=chips[slot];i++;
  const name=e.symbol?'$'+e.symbol:(e.side==='long'?'多單':'空單');
  $('replay').textContent=`${tsd(e.ts)} · ${(i-1)%list.length+1}/${list.length}`;
  ch.t.textContent=name;ch.s.textContent=e.chip;ch.r.setAttribute('fill','#18202c');
  act('scan');$('why').innerHTML=`<b>${esc(name)}</b> ${esc(e.context)}`;
  await travel(chipPath(slot),ch.c,900);
  act('risk');flash(RK);RK.st.textContent='檢查 '+name;
  if(!e.approved){const el=$('ag-risk');if(el){el.classList.add('stamped');setTimeout(()=>el.classList.remove('stamped'),1700)}
   RK.st.textContent='FILTER '+name;act('log');
   $('why').innerHTML=`<b style="color:var(--r)">攔下 ${esc(name)}</b>　${esc(e.why)}`;
   await W(2200)}
  else{RK.st.textContent='PASS '+name;act('size');
   await travel(`M${RN.x+30},${RN.y} L${EN.x-30},${EN.y}`,'#38d6e8',800);
   flash(EX);EX.st.textContent=`${(e.side||'').toUpperCase()} ${name}`;act('exec',2400);act('log');
   $('why').innerHTML=`<b style="color:var(--c)">通過 ${esc(name)}</b>　${esc(e.why)}`;
   await W(3000);EX.st.textContent='待命'}
  ch.r.setAttribute('fill','#10151d');
 }}

load();setInterval(load,30000);replay();

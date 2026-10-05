"use strict";

const $ = id => document.getElementById(id);
const clone = value => JSON.parse(JSON.stringify(value));
const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fieldNames = {open:"开盘价",high:"最高价",low:"最低价",close:"收盘价",volume:"成交量"};
const opNames = {gt:"严格高于",lt:"严格低于",cross_up:"向上穿越",cross_down:"向下穿越",and:"AND · 同时满足",or:"OR · 任一满足"};
const numericOps = ["field","const","ma","ema","rsi","bollinger","macd","std","lag","add","sub","mul","div"];
const arithmeticNames = {add:"加法",sub:"减法",mul:"乘法",div:"除法（除零不可用）"};
const numericKeys = ["window","value","multiplier","fast","slow","signal"];
const indicatorNames = {ma:"移动平均线 MA",ema:"指数移动平均线 EMA",rsi:"相对强弱指数 RSI",bollinger:"布林带",macd:"指数平滑异同移动平均线 MACD",const:"数值常量",std:"滚动标准差 STD",lag:"历史滞后 LAG",...arithmeticNames};
const bandNames = {upper:"上轨",middle:"中轨",lower:"下轨"};
const componentNames = {line:"DIF",signal:"DEA",histogram:"柱值 DIF−DEA"};
const state = {bootstrap:null,strategy:null,positions:{},revision:0,importSequence:0,valid:false,history:[],generationId:null,candidate:null,result:null,runId:null,page:0,runBusy:false,aiBusy:false};
let validationTimer;

function notice(message, error=false) {
  $("notice").textContent = message; $("notice").className = "notice" + (error ? " error" : ""); $("notice").hidden = false;
}

async function api(path, body, extraHeaders={}) {
  const response = await fetch(path, body === undefined ? {} : {method:"POST",headers:{"Content-Type":"application/json","X-FinBlocks-Token":state.bootstrap.token,...extraHeaders},body:JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "请求未完成");
  return result;
}

function config() {
  return {symbol:isPortfolio()?portfolioCodes()[0]||$("symbol").value:$("symbol").value,start:$("start").value || null,end:$("end").value || null,cost_bps:$("cost").value === "" ? null : Number($("cost").value),lag:Number($("lag").value),...(isPortfolio()?{mode:"portfolio",symbols:portfolioCodes(),portfolio:portfolioOptions()}:{}),
    ...($("data-source")?.value==="public_hfq"?{data_source:"public_hfq"}:{}),periods_per_year:$("periods").value === "" ? null : Number($("periods").value),annual_risk_free_rate:$("risk-free").value === "" ? null : Number($("risk-free").value)/100};
}

function updateDataSourceInfo(){
  const info=state.bootstrap.data_sources?.public_hfq;
  if($("data-source-info"))$("data-source-info").textContent=$("data-source").value==="public_hfq"?`公开快照通过 ${info?.summary.ready||0} 股；价格为复权研究单位，部分标的回退day并单独核验。成交量已换算为股，市值留空。各股覆盖不同，请检查集合；无可用数据时阻断，不能当作实盘订单价格。`:"原始ZIP保持不变；除权参考价断点与缺失值继续严格检查。";
}

function selectedSymbol(){return state.bootstrap.symbols.find(s=>s.code===$("symbol").value);}
function canRunSelected(){if(isPortfolio())return !portfolioState.busy&&portfolioState.check?.status==="PASS";const symbol=selectedSymbol();return !!symbol&&symbol.local_data!==false;}

function filteredSymbols(){
  const pool=$("stock-pool").value||"demo",query=$("symbol-search").value.trim().toLowerCase();
  return state.bootstrap.symbols.filter(s=>(s.pool||"demo")===pool&&(!query||(s.name+" "+s.code).toLowerCase().includes(query)));
}

function renderSymbolOptions(preferred=$("symbol").value,chooseFirst=false){
  const matches=filteredSymbols();
  $("symbol").innerHTML='<option value="">请选择股票</option>'+matches.map(s=>`<option value="${s.code}">${escapeHtml(s.name)} · ${s.code}${s.local_data===false?" · 暂无本地行情":""}</option>`).join("");
  $("symbol").value=matches.some(s=>s.code===preferred)?preferred:chooseFirst?(matches[0]?.code||""):"";
  $("symbol").disabled=!matches.length;
  $("search-status").textContent=matches.length?`找到 ${matches.length} 个标的；按名称或代码搜索。`:"没有找到匹配股票，请修改搜索词。";
}

function updateSymbolInfo(){
  updateDataSourceInfo();
  const symbol=selectedSymbol(),pool=state.bootstrap.stock_pool;
  $("pool-info").textContent=$("stock-pool").value==="csi300"&&pool?`中证指数官方名单 · ${pool.as_of} · ${pool.summary.official_members} 个成员。当前名单回看历史，非历史逐日成分池；此处每次检验一只股票。`:"历史演示标的，用于复核已有策略；不计入沪深300成员。";
  if(isPortfolio())updatePortfolioMode();
  $("checked-range-btn").hidden=true;
  if(!symbol){$("symbol-quality").textContent="请选择一只股票，再设置历史区间。";return;}
  if($("data-source")?.value==="public_hfq"){
    const available=state.bootstrap.data_sources?.public_hfq?.members[symbol.code];
    $("symbol-quality").textContent=available?.status==="READY"?`公开快照：${available.start} 至 ${available.end}，${available.rows} 条；回测使用研究价格。`:"此标的公开数据未就绪，不能回退原始数据。";return;
  }
  if(symbol.local_data===false){$("symbol-quality").textContent="本地行情暂不可读："+(symbol.quality?.error||"尚未核验");return;}
  const quality=symbol.quality,checked=quality?.checked_range;
  let message=`本地行情：${symbol.start} 至 ${symbol.end}，${symbol.rows} 条。`;
  if(quality?.full_history_passed===false){message+=` 完整历史有 ${quality.issue_rows??"待核验的"} 条价格问题，不能直接回测完整区间。`;
    if(checked){message+=` 已检查区间：${checked.start} 至 ${checked.end}（${checked.rows} 条）。`;
      $("checked-range-btn").hidden=false;
      if($("start").value===checked.start&&$("end").value===checked.end)message+=" 当前已采用该区间；仍需满足策略窗口。";
    }else message+=" 暂无足够长的已检查近期区间，可调整日期后重新检查。";
  }else message+=" 区间价格检查通过；不代表完整复权已核验。";
  $("symbol-quality").textContent=message;
}

function changeSymbol(resetDates=true){
  const symbol=selectedSymbol();
  const available=$("data-source")?.value==="public_hfq"?state.bootstrap.data_sources?.public_hfq?.members[symbol?.code]:symbol;
  if(resetDates&&!isPortfolio()){$("start").value=available?.start||"";$("end").value=available?.end||"";}
  $("financial-table").hidden=true;$("financial-summary").textContent="已切换标的，请重新查看对应财报。";
  updateSymbolInfo();invalidate();validateCurrent();
}

function snapshot() { return {strategy:clone(state.strategy),positions:clone(state.positions),generationId:state.generationId}; }
function remember() {state.history.push(snapshot()); if(state.history.length>40) state.history.shift(); $("undo-btn").disabled=false;}

function invalidate() {
  state.revision++; state.valid=false; state.result=null; state.runId=null; state.page=0;state.chartGeometry=null;state.chartFocusIndex=null;
  if(typeof resetResearch==="function")resetResearch();
  clearPortfolioCheck();
  ["export-json","export-csv","export-chart","run-btn"].forEach(id => $(id).disabled=true);
  ["metric-return","metric-benchmark","metric-drawdown","metric-trades","metric-sharpe","metric-benchmark-sharpe","metric-volatility","metric-annual-return"].forEach(id => {$(id).textContent="—";$(id).className="";$(id).title="";});
  $("result-subtitle").textContent="当前配置尚未回测。修改后旧结果已失效。";
  $("chart-empty").hidden=false; $("chart-tooltip").hidden=true;
  $("chart-readout").textContent="当前配置尚未回测，没有可读取的净值。";
  $("assumptions-content").textContent="运行后记录实际数据范围、源文件哈希与执行假设。";
  $("validation").className="validation pending";$("validation").textContent="正在校验当前策略…";
  renderLedger(); drawChart();
}

function changed(render=true) { invalidate(); if(render)renderGraph();else $("dsl-view").textContent=JSON.stringify(state.strategy,null,2); clearTimeout(validationTimer); validationTimer=setTimeout(validateCurrent,180); }
function nodeType(node) {return ["field","const"].includes(node.op) ? "field" : numeric(node) ? "ma" : ["and","or"].includes(node.op) ? "logic" : "condition";}
function numeric(node) {return numericOps.includes(node.op);}
function label(node) {if(node.op==="field")return fieldNames[node.field]||node.field;if(node.op==="const")return "常量 "+node.value;if(["ma","ema","rsi","std","lag"].includes(node.op))return node.op.toUpperCase()+" "+node.window;if(node.op==="bollinger")return `布林 ${bandNames[node.band]} ${node.window}×${node.multiplier}`;if(node.op==="macd")return `MACD ${componentNames[node.component]} ${node.fast}/${node.slow}/${node.signal}`;return opNames[node.op]||arithmeticNames[node.op]||node.op;}
function options(items, selected) {
  const list = items.map(([value,text]) => `<option value="${escapeHtml(value)}"${value === selected ? " selected" : ""}>${escapeHtml(text)}</option>`);
  if(!items.some(([value]) => value === selected)) list.unshift(`<option value="${escapeHtml(selected || "")}" selected>${selected ? "缺失引用："+escapeHtml(selected) : "请选择"}</option>`);
  return list.join("");
}

function refs(node, kind) {return state.strategy.nodes.filter(n => n.id!==node.id && (kind === "number" ? numeric(n) : !numeric(n))).map(n => [n.id,n.id+" · "+label(n)]);}
function control(title, key, content, input=false) {const limits=key==="value"?'step="any"':key==="multiplier"?'min="0.01" max="10" step="0.1"':'min="2" max="500" step="1"';return `<div><label>${title}</label>${input ? `<input type="number" ${limits} data-key="${key}" aria-label="${title}" value="${escapeHtml(content)}">` : `<select data-key="${key}" aria-label="${title}">${content}</select>`}</div>`;}

function defaultPositions() {
  const depths=new Map();
  const depth = (id, visited=new Set()) => {
    if(visited.has(id))return 0;
    if(depths.has(id))return depths.get(id);
    const node=state.strategy.nodes.find(n=>n.id===id); if(!node)return 0;
    const inputs=[node.input,node.left,node.right].filter(Boolean);
    const value=inputs.length ? 1+Math.max(...inputs.map(ref=>depth(ref,new Set([...visited,id])))) : 0;depths.set(id,value);return value;
  };
  const columns={};
  state.positions={};
  state.strategy.nodes.forEach(node=>{const col=Math.min(depth(node.id),4);const y=columns[col]??(col===0?130:35);state.positions[node.id]={x:20+col*240,y};columns[col]=y+(node.op==="macd"?340:node.op==="bollinger"?290:200);});
}

function renderGraph() {
  $("strategy-name").value=state.strategy.name;
  $("signal").innerHTML=options(state.strategy.nodes.filter(n=>!numeric(n)).map(n=>[n.id,n.id+" · "+label(n)]),state.strategy.signal);
  $("true-weight").value=state.strategy.allocation.when_true ?? ""; $("false-weight").value=state.strategy.allocation.when_false ?? "";
  $("dsl-view").textContent=JSON.stringify(state.strategy,null,2);
  const icons={field:"↗",ma:"ƒ",condition:"⋈",logic:"&"};
  $("nodes").innerHTML=state.strategy.nodes.map((node,i)=>{
    if(!state.positions[node.id]) state.positions[node.id]={x:20+(i%3)*240,y:30+Math.floor(i/3)*215};
    const type=nodeType(node);let body;
    if(node.op==="field")body=control("输入字段","field",options(Object.entries(fieldNames),node.field));
    else if(node.op==="const")body=control("常量值","value",node.value,true);
    else if(["ma","ema","rsi","std","lag"].includes(node.op))body=control("输入积木","input",options(refs(node,"number"),node.input))+control("窗口/历史滞后（记录数）","window",node.window,true);
    else if(arithmeticNames[node.op])body=control("算术操作","op",options(Object.entries(arithmeticNames),node.op))+control("左侧输入","left",options(refs(node,"number"),node.left))+control("右侧输入","right",options(refs(node,"number"),node.right));
    else if(node.op==="bollinger")body=control("输入积木","input",options(refs(node,"number"),node.input))+control("窗口（记录数）","window",node.window,true)+control("标准差倍数","multiplier",node.multiplier,true)+control("布林轨道","band",options(Object.entries(bandNames),node.band));
    else if(node.op==="macd")body=control("输入积木","input",options(refs(node,"number"),node.input))+control("快线窗口","fast",node.fast,true)+control("慢线窗口","slow",node.slow,true)+control("信号线窗口","signal",node.signal,true)+control("输出分量","component",options(Object.entries(componentNames),node.component));
    else {const logic=type==="logic"; body=control("条件类型","op",options(Object.entries(opNames).filter(([op])=>logic === ["and","or"].includes(op)),node.op))+control("左侧输入","left",options(refs(node,logic?"boolean":"number"),node.left))+control("右侧输入","right",options(refs(node,logic?"boolean":"number"),node.right));}
    return `<article class="node" data-id="${escapeHtml(node.id)}" data-type="${type}"><div class="node-header" data-handle="${escapeHtml(node.id)}"><span class="node-icon">${icons[type]}</span><b>${indicatorNames[node.op]||(type==="field"?"行情字段":type==="logic"?"逻辑组合":"比较条件")}</b>${termHelp(TERMS[node.op]?node.op:type==="logic"?"logic":"condition")}<span class="node-id" title="${escapeHtml(node.id)}">${escapeHtml(node.id)}</span><button class="node-delete" data-delete="${escapeHtml(node.id)}" aria-label="删除积木 ${escapeHtml(node.id)}">×</button></div><div class="node-body">${body}</div></article>`;
  }).join("");
  document.querySelectorAll(".node").forEach(card=>{const pos=state.positions[card.dataset.id];card.style.left=pos.x+"px";card.style.top=pos.y+"px";});
  const bottom=Math.max(355,...Array.from(document.querySelectorAll(".node"),card=>state.positions[card.dataset.id].y+card.offsetHeight+30));
  const right=Math.max(710,...Object.values(state.positions).map(p=>p.x+205));
  $("graph").style.height=bottom+"px";$("graph").style.width=right+"px";
  requestAnimationFrame(drawWires);
}

function drawWires() {
  const lines=[];
  state.strategy.nodes.forEach(node=>{const target=document.querySelector(`.node[data-id="${node.id}"]`);if(!target)return;
    [node.input,node.left,node.right].filter(Boolean).forEach(id=>{const source=document.querySelector(`.node[data-id="${id}"]`);if(!source)return;
      const a=state.positions[id],b=state.positions[node.id];const x1=a.x+source.offsetWidth,y1=a.y+source.offsetHeight/2,x2=b.x,y2=b.y+target.offsetHeight/2;const bend=Math.max(35,Math.abs(x2-x1)*.5);
      lines.push(`<path d="M${x1} ${y1} C${x1+bend} ${y1},${x2-bend} ${y2},${x2} ${y2}"/>`);
    });
  });
  $("wires").innerHTML=lines.join("");
}

async function validateCurrent() {
  if(!state.strategy)return false;
  const rev=state.revision;
  try {const result=await api("/api/validate",{strategy:state.strategy});if(rev!==state.revision)return false;
    state.valid=true;$("validation").className="validation good";$("validation").textContent=`✓ 策略校验通过 · ${result.ordered_ids.length} 个有效积木 · 指标需 ${result.warmup_records} 条历史记录`;
    document.querySelectorAll(".node.error").forEach(node=>node.classList.remove("error"));$("run-btn").disabled=state.runBusy||!canRunSelected();return true;
  }catch(error){if(rev!==state.revision)return false;state.valid=false;$("validation").className="validation bad";$("validation").textContent="校验未通过："+error.message;$("run-btn").disabled=true;
    document.querySelectorAll(".node").forEach(node=>node.classList.toggle("error",error.message.includes(node.dataset.id+"：")||error.message.includes("未连接")));return false;
  }
}

function addNode(op, position) {
  remember();let i=1;while(state.strategy.nodes.some(n=>n.id==="block"+i))i++;const id="block"+i;
  const numbers=state.strategy.nodes.filter(numeric),booleans=state.strategy.nodes.filter(n=>!numeric(n));
  let node=op==="field"?{id,op,field:"close"}:op==="const"?{id,op,value:0}:["ma","ema","rsi"].includes(op)?{id,op,input:numbers[0]?.id||"",window:op==="rsi"?14:20}:op==="bollinger"?{id,op,input:numbers[0]?.id||"",window:20,multiplier:2,band:"upper"}:op==="macd"?{id,op,input:numbers[0]?.id||"",fast:12,slow:26,signal:9,component:"line"}:op==="and"?{id,op,left:booleans[0]?.id||"",right:booleans[1]?.id||""}:{id,op,left:numbers[0]?.id||"",right:numbers[1]?.id||""};
  if(!position){let row=0;outer:for(;row<30;row++){for(let col=0;col<3;col++){const p={x:20+col*240,y:30+row*340};if(!Object.values(state.positions).some(q=>Math.abs(p.x-q.x)<200&&Math.abs(p.y-q.y)<330)){position=p;break outer;}}}}
  state.strategy.nodes.push(node);state.positions[id]=position||{x:20,y:400};changed();
  notice("积木已添加。请选择输入，并将它接入交易条件；未使用的积木不能通过校验。");
}

function workspaceFile(){return {format:"finblocks-workspace",version:1,strategy:clone(state.strategy),config:config(),view:{positions:clone(state.positions)}};}
function workspaceStorageKey(){return state.bootstrap.user?"finblocks-workspace-v1-user-"+state.bootstrap.user.id:"finblocks-workspace-v1";}
async function exportArtifact(payload) {const identity=identityRevision;const file=await api("/api/export-artifact",payload);if(identity!==identityRevision)throw new Error("导出期间账号已切换，请按当前账号重新操作");notice(`文件已实际保存：artifacts/web/${file.filename}。`);const link=document.createElement("a");link.href=file.url;link.download="finblocks-"+file.filename.slice(33);link.textContent=" 下载文件";$("notice").append(link);link.click();return file;}

async function saveWorkspace(){if(!await validateCurrent()){notice("策略尚未通过校验，请先修复后保存。",true);return;}const saved=workspaceFile(),key=workspaceStorageKey();await exportArtifact({kind:"workspace",workspace:saved});localStorage.setItem(key,JSON.stringify(saved));}

function describeStrategy(strategy){const table=Object.fromEntries(strategy.nodes.map(n=>[n.id,n]));let budget=64;const expression=id=>{if(--budget<0)return `积木 ${id}`;const n=table[id];if(n.op==="field")return fieldNames[n.field];if(n.op==="const")return String(n.value);if(arithmeticNames[n.op])return `(${expression(n.left)} ${arithmeticNames[n.op]} ${expression(n.right)})`;if(numeric(n))return `${label(n)}(${expression(n.input)})`;return `(${expression(n.left)} ${opNames[n.op]} ${expression(n.right)})`;};return `${expression(strategy.signal)} 时持仓 ${strategy.allocation.when_true*100}%，否则 ${strategy.allocation.when_false*100}%。穿越条件只在发生穿越的记录为真。`;}

async function applyWorkspace(document, imported=true) {
  const sequence=++state.importSequence,rev=state.revision;
  let strategy, settings, positions;
  if(document.format==="finblocks-workspace"){if(document.version!==1)throw new Error("不支持的工作台文件版本");strategy=document.strategy;settings=document.config;positions=document.view?.positions;}
  else if(document.status==="ok"&&document.strategy)strategy=document.strategy;
  else strategy=document;
  await api("/api/validate",{strategy});
  if(sequence!==state.importSequence||rev!==state.revision)throw new Error("导入校验期间策略或配置已改变，本次导入未应用，请重新导入");
  if(settings){settings={periods_per_year:252,annual_risk_free_rate:0,...settings};if(!Number.isInteger(settings.periods_per_year)||settings.periods_per_year<1||settings.periods_per_year>366||typeof settings.annual_risk_free_rate!=="number"||!Number.isFinite(settings.annual_risk_free_rate)||settings.annual_risk_free_rate<=-1||settings.annual_risk_free_rate>1)throw new Error("导入的年化周期或无风险利率无效");}
  if(settings?.mode==="portfolio")validPortfolioSettings(settings);else if(settings&&(settings.mode&&settings.mode!=="single"||settings.symbols||settings.portfolio))throw new Error("导入回测模式或组合设置不一致");
  if(settings?.data_source&&!['original','public_hfq'].includes(settings.data_source))throw new Error("导入数据源不受支持");
  if(settings?.data_source==="public_hfq"&&!state.bootstrap.data_sources?.public_hfq)throw new Error("该公开快照尚未就绪");
  if(settings){if(!state.bootstrap.symbols.some(s=>s.code===settings.symbol))throw new Error("导入的标的未在当前股票池中");if(![1,2].includes(settings.lag))throw new Error("界面仅支持滞后1或2条记录");if(typeof settings.cost_bps!=="number"||!Number.isFinite(settings.cost_bps)||settings.cost_bps<0||settings.cost_bps>=10000)throw new Error("导入的成本配置无效");for(const day of [settings.start,settings.end])if(day!==null&&(!/^\d{4}-\d{2}-\d{2}$/.test(day)||Number.isNaN(Date.parse(day))||new Date(day).toISOString().slice(0,10)!==day))throw new Error("导入的日期格式无效");if(settings.start&&settings.end&&settings.start>settings.end)throw new Error("导入的开始日期晚于结束日期");}
  if(state.strategy)remember();state.strategy=clone(strategy);state.generationId=null;defaultPositions();
  if(positions)for(const node of state.strategy.nodes){const p=positions[node.id];if(p&&Number.isFinite(p.x)&&Number.isFinite(p.y)&&p.x>=0&&p.x<=3000&&p.y>=0&&p.y<=5000)state.positions[node.id]={x:p.x,y:p.y};}
  if(settings){$("stock-pool").value=state.bootstrap.symbols.find(s=>s.code===settings.symbol).pool||"demo";$("symbol-search").value="";renderSymbolOptions(settings.symbol);$("start").value=settings.start||"";$("end").value=settings.end||"";$("cost").value=settings.cost_bps;$("lag").value=settings.lag;$("periods").value=settings.periods_per_year;$("risk-free").value=settings.annual_risk_free_rate*100;updateSymbolInfo();$("financial-table").hidden=true;$("financial-summary").textContent="策略已恢复，请重新查看对应财报。";}
  if(settings){$("data-source").value=settings.data_source||"original";applyPortfolioSettings(settings);updateSymbolInfo();}
  changed();if(imported)notice("策略已导入并校验通过。导入文件不被自动标为本次真实AI调用。"+(isPortfolio()?"请重新检查股票池数据。":""));
}

async function generate(){if(state.aiBusy)return;const identity=identityRevision;state.aiBusy=true;state.candidate=null;$("ai-candidate").hidden=true;$("generate-btn").disabled=true;$("generate-btn").textContent="正在调用真实模型…";
  try {const result=await api("/api/generate",{prompt:$("ai-prompt").value});if(identity!==identityRevision)return;$("ai-candidate").hidden=false;$("ai-status").textContent=result.status==="ok"?"AI 已实际调用 · 输出经编译校验":"AI 已实际调用 · 需求被拒绝";
    if(result.status!=="ok"){$("candidate-title").textContent="该需求暂不支持";$("candidate-description").textContent=result.reason;$("apply-ai-btn").hidden=true;notice("实际AI拒绝了该需求，当前策略未被修改。",true);return;}
    state.candidate=result;$("candidate-title").textContent="候选策略已生成 · 尚未应用";$("candidate-description").textContent=`${describeStrategy(result.strategy)} 模型命名：${result.strategy.name}（以积木条件为准）。模型：${result.evidence.model_returned}；实际用量：${result.evidence.usage?.total_tokens??"未返回"} token。`;
    $("apply-ai-btn").hidden=false;$("apply-ai-btn").disabled=!!result.intent_audit?.application_blocked;
    if(typeof renderIntentAudit==="function"&&result.intent_audit)renderIntentAudit(result.intent_audit);
    notice(result.intent_audit?.application_blocked?"候选存在意图差异或不支持的持仓语义，应用已阻断，请先明确需求。":"真实AI输出已通过编译校验。核对意图后应用到画布，再运行回测。",!!result.intent_audit?.application_blocked);
  }catch(error){if(identity===identityRevision)notice("AI生成失败："+error.message,true);}finally{state.aiBusy=false;$("generate-btn").disabled=!state.bootstrap.ai_configured;$("generate-btn").textContent="生成策略候选";}
}

async function run(){if(state.runBusy||!canRunSelected())return;state.runBusy=true;$("run-btn").disabled=true;
  try {if(!await validateCurrent())return;const rev=state.revision;$("run-btn").textContent="正在读取真实数据…";const result=await api(isPortfolio()?"/api/portfolio/backtest":"/api/backtest",isPortfolio()?portfolioPayload():{strategy:state.strategy,...config(),generation_id:state.generationId});if(rev!==state.revision){notice("运行期间配置已改变，本次结果未应用，请按当前配置重新回测。",true);return;}
    state.result=result.report;state.runId=result.run_id;state.page=0;renderResults();notice("真实回测完成。导出报告、逐记录账目和净值图均来自本次运行。");
  }catch(error){notice("回测未完成："+error.message,true);}finally{state.runBusy=false;$("run-btn").disabled=!state.valid||!canRunSelected();$("run-btn").textContent="▶ 运行真实回测";}
}

function percent(value){return (value*100).toFixed(2)+"%";}
function renderFundamentals(){const indicators=state.bootstrap.fundamentals.indicators;$("fundamental-status").innerHTML=indicators.map(item=>`<span title="${escapeHtml(item.reason)}">${escapeHtml(item.label)} · 暂不可回测<br>${escapeHtml(item.reason)}</span>`).join("");}
async function loadFinancials(){const symbol=$("symbol").value;$("financial-btn").disabled=true;$("financial-summary").textContent="正在读取本机原始财报…";
  try{const report=await api("/api/financials",{symbol});if(symbol!==$("symbol").value)return;$("financial-summary").textContent=`${symbol} · ${report.rows} 条原始发布记录，展示最近 12 条（保留修订版本）。${report.limitations} 来源：${report.source.member}`;
    $("financial-table").innerHTML=`<table><thead><tr><th>报告期（原值）</th><th>发布日期</th>${report.fields.map(f=>`<th>${escapeHtml(f)}</th>`).join("")}</tr></thead><tbody>${report.reports.slice(-12).reverse().map(row=>`<tr><td>${escapeHtml(row.report_date)}</td><td>${escapeHtml(row.publish_date)}</td>${report.fields.map(f=>`<td>${escapeHtml(row.values[f]||"—")}</td>`).join("")}</tr>`).join("")}</tbody></table>`;$("financial-table").hidden=false;
  }catch(error){$("financial-summary").textContent="财报读取未完成："+error.message;}finally{$("financial-btn").disabled=false;}}
function renderResults(){const r=state.result,m=r.metrics;const symbol=r.kind==="portfolio"?{name:`${r.symbols.length}股共享现金组合`}:state.bootstrap.symbols.find(s=>s.code===$("symbol").value);
  for(const [id,value] of [["metric-return",m.total_return],["metric-benchmark",m.buy_hold_return],["metric-drawdown",m.max_drawdown]]){$(id).textContent=percent(value);$(id).className=value<0?"negative":"positive";}
  $("metric-trades").textContent=m.trade_records.toLocaleString();$("result-subtitle").textContent=`${symbol.name} · ${m.start} — ${m.end} · ${m.rows.toLocaleString()} 条真实记录 · 成本 ${r.assumptions.cost_bps} 基点 · 信号滞后 ${r.assumptions.execution_lag_bars} 条记录`;
  for(const [id,key,ratio] of [["metric-sharpe","sharpe_ratio",true],["metric-benchmark-sharpe","buy_hold_sharpe_ratio",true],["metric-volatility","annualized_volatility",false],["metric-annual-return","annualized_return",false]]){const value=m[key];$(id).textContent=Number.isFinite(value)?ratio?value.toFixed(3):percent(value):"—";$(id).title=m[key==="sharpe_ratio"?"sharpe_unavailable_reason":key==="buy_hold_sharpe_ratio"?"buy_hold_sharpe_unavailable_reason":key+"_unavailable_reason"]||"";$(id).className=Number.isFinite(value)?value<0?"negative":"positive":"";}
  $("result-subtitle").textContent+=` · 年化 ${r.assumptions.periods_per_year} 区间 · 无风险 ${percent(r.assumptions.annual_risk_free_rate)}`;
  if(r.source.source_id==="public_hfq"||r.source.data_source==="public_hfq")$("result-subtitle").textContent+=" · 公开快照 · 复权研究价格";
  $("chart-empty").hidden=true;["export-json","export-csv","export-chart"].forEach(id=>$(id).disabled=false);
  $("chart").setAttribute("aria-label",`${symbol.name}，${m.start}至${m.end}，${m.rows}条真实回测记录的净值图`);$("chart-readout").textContent="使用左右方向键查看日期与实际净值，Home/End查看首末记录。";
  const sourceText=r.kind==="portfolio"?r.source.members.map(s=>`${s.name} ${s.code} / ${s.member} / SHA-256 ${s.member_sha256}`).join("\n"):`${r.source.member}\n源成员 SHA-256：${r.source.member_sha256}`;
  $("assumptions-content").textContent=`来源：${r.source.archive} / ${sourceText}\n原数据包 SHA-256：${r.source.archive_sha256}\n策略来源：${r.strategy_origin.type==="actual_api"?"本次实际AI生成":r.strategy_origin.type==="actual_api_then_edited"?"实际AI生成后人工编辑":"人工或导入策略"}\n${r.assumptions.execution}\n${r.assumptions.initial_capital}；现金利息为零；末日不强制清仓。\n${r.assumptions.cost_basis}\n${r.assumptions.price_basis}\n未建模：${r.assumptions.not_modelled.join("、")}。样本末尾未执行信号：${JSON.stringify(r.last_signal_not_executed_within_sample)}`;
  $("assumptions-content").textContent+=`\n${r.assumptions.statistics_scope}\n${r.assumptions.annualization_basis}\n夏普公式：${r.assumptions.sharpe_formula}\n${r.assumptions.annualization_limitations}\n夏普不可用说明：${m.sharpe_unavailable_reason||"无"}`;
  if(r.source.source_id==="public_hfq"||r.source.data_source==="public_hfq")$("assumptions-content").textContent+="\n公开来源响应、获取时间、单位证据、逐股价格口径及SHA均在JSON报告source中；原数据包SHA只用于记录原始基线，不代表本次价格来源。";
  if(r.stock_pool)$("assumptions-content").textContent+=`\n股票池：${r.kind==="portfolio"?"明确组合集合 · 当前名单日期 "+r.stock_pool.as_of:r.stock_pool.membership==="csi300"?"沪深300 · 名单日期 "+r.stock_pool.as_of:"历史演示标的"}\n${r.stock_pool.limitations}`;
  if(r.kind==="portfolio")$("assumptions-content").textContent+=`\n${r.assumptions.selection}\n${r.assumptions.allocation}\n${r.assumptions.exit}\n${r.assumptions.calendar}\n基准：${r.assumptions.benchmark}\n组合设置：${JSON.stringify(r.portfolio)}`;
  renderLedger();drawChart();renderPortfolioResult();
}

function ledgerRows(){return state.result?state.result.records.filter(r=>!$("trade-only").checked||r.traded_notional>1e-12):[];}
function renderLedger(){const rows=ledgerRows(),size=12,total=Math.max(1,Math.ceil(rows.length/size));state.page=Math.max(0,Math.min(state.page,total-1));const page=rows.slice(state.page*size,(state.page+1)*size);
  $("ledger").innerHTML=page.length?page.map(r=>`<tr><td><button class="text-button" data-evidence-date="${r.date}">${r.date}</button></td><td>${r.executed_signal_date||"—"}</td><td>${r.signal===null?"预热":r.signal?"真":"假"}</td><td>${(r.target_weight*100).toFixed(0)}%</td><td>${r.equity.toFixed(4)}</td><td>${r.fee.toFixed(6)}</td></tr>`).join(""):'<tr><td colspan="6" class="table-empty">'+(state.result?"当前筛选无记录":"尚无执行记录")+'</td></tr>';
  $("page-info").textContent=rows.length?`${rows.length.toLocaleString()} 条记录 · 第 ${state.page+1} / ${total} 页`:"0 条记录";$("previous-page").disabled=state.page===0;$("next-page").disabled=state.page>=total-1;
}

function drawChart(hoverIndex=null){const canvas=$("chart"),rect=canvas.getBoundingClientRect(),ratio=window.devicePixelRatio||1;canvas.width=rect.width*ratio;canvas.height=rect.height*ratio;const ctx=canvas.getContext("2d");ctx.scale(ratio,ratio);ctx.fillStyle="#fff";ctx.fillRect(0,0,rect.width,rect.height);state.chartGeometry=null;if(!state.result||!state.result.records.length||rect.width<90)return;
  ctx.font='11px "Segoe UI","Microsoft YaHei"';ctx.fillStyle="#087969";ctx.fillText("FinBlocks · 归一化净值",49,16);ctx.fillStyle="#526f82";if(rect.width>450)ctx.fillText(`成本 ${state.result.assumptions.cost_bps} 基点 · 滞后 ${state.result.assumptions.execution_lag_bars} 条`,Math.max(260,rect.width-205),16);
  ctx.font='10px "Segoe UI","Microsoft YaHei"';ctx.fillText(state.result.kind==="portfolio"?(rect.width<550?"组合策略（实线） · 等权基准（虚线）":"组合策略（实线） · 股票池等权买入持有（虚线）"):"当前策略（实线） · 买入持有（虚线）",49,32);
  const records=state.result.records,w=rect.width,h=rect.height,pad={left:49,right:16,top:41,bottom:28};let min=Infinity,max=-Infinity;for(const r of records){min=Math.min(min,r.equity,r.buy_hold_equity);max=Math.max(max,r.equity,r.buy_hold_equity);}const range=Math.max(max-min,.1);min=Math.max(0,min-range*.08);max+=range*.08;
  const x=i=>pad.left+i/Math.max(1,records.length-1)*(w-pad.left-pad.right),y=v=>pad.top+(max-v)/(max-min)*(h-pad.top-pad.bottom);
  ctx.font='11px "Segoe UI","Microsoft YaHei"';ctx.textBaseline="middle";ctx.fillStyle="#5c7887";ctx.strokeStyle="#e7eef2";ctx.lineWidth=1;
  for(let i=0;i<=4;i++){const value=min+(max-min)*i/4,yy=y(value);ctx.fillText(value.toFixed(2),3,yy);ctx.beginPath();ctx.moveTo(pad.left,yy);ctx.lineTo(w-pad.right,yy);ctx.stroke();}
  const dateTicks=w<550?2:4;for(let i=0;i<=dateTicks;i++){const index=Math.round((records.length-1)*i/dateTicks),xx=x(index);ctx.textAlign=i===0?"left":i===dateTicks?"right":"center";ctx.fillText(records[index].date,xx,h-12);}ctx.textAlign="left";
  function line(key,color,dashed){ctx.beginPath();records.forEach((r,i)=>i===0?ctx.moveTo(x(i),y(r[key])):ctx.lineTo(x(i),y(r[key])));ctx.strokeStyle=color;ctx.lineWidth=dashed?1.4:2;ctx.setLineDash(dashed?[5,4]:[]);ctx.stroke();ctx.setLineDash([]);}
  line("buy_hold_equity","#6681a0",true);line("equity","#087969",false);state.chartGeometry={x,y,w,h,pad};
  if(Number.isInteger(hoverIndex)&&hoverIndex>=0&&hoverIndex<records.length){const row=records[hoverIndex],xx=x(hoverIndex);ctx.beginPath();ctx.moveTo(xx,pad.top);ctx.lineTo(xx,h-pad.bottom);ctx.setLineDash([3,4]);ctx.strokeStyle="#809caa";ctx.lineWidth=1;ctx.stroke();ctx.setLineDash([]);for(const [key,color] of [["equity","#087969"],["buy_hold_equity","#6681a0"]]){ctx.beginPath();ctx.arc(xx,y(row[key]),4,0,Math.PI*2);ctx.fillStyle="#fff";ctx.fill();ctx.strokeStyle=color;ctx.lineWidth=2;ctx.stroke();}}
}

function showChartPoint(index){if(!state.result||!state.chartGeometry)return;const records=state.result.records;index=Math.max(0,Math.min(records.length-1,index));const r=records[index];state.chartFocusIndex=index;drawChart(index);const g=state.chartGeometry,tooltip=$("chart-tooltip");tooltip.textContent=`${r.date}　策略 ${r.equity.toFixed(4)}　买入持有 ${r.buy_hold_equity.toFixed(4)}`;tooltip.hidden=false;tooltip.style.left=Math.max(5,Math.min(g.x(index)-90,g.w-tooltip.offsetWidth-5))+"px";tooltip.style.top="24px";$("chart-readout").textContent=tooltip.textContent;}

function bind(){document.querySelectorAll("[data-add]").forEach(button=>{button.addEventListener("click",()=>addNode(button.dataset.add));button.addEventListener("dragstart",event=>event.dataTransfer.setData("text/plain",button.dataset.add));});
  $("board").addEventListener("dragover",event=>event.preventDefault());$("board").addEventListener("drop",event=>{event.preventDefault();const op=event.dataTransfer.getData("text/plain");if(![...numericOps,"gt","and"].includes(op))return;const rect=$("graph").getBoundingClientRect();addNode(op,{x:Math.max(10,event.clientX-rect.left-80),y:Math.max(10,event.clientY-rect.top-25)});});
  $("nodes").addEventListener("change",event=>{const key=event.target.dataset.key,card=event.target.closest(".node");if(!key||!card)return;const node=state.strategy.nodes.find(n=>n.id===card.dataset.id),value=numericKeys.includes(key)?(event.target.value===""?null:Number(event.target.value)):event.target.value;if(node[key]===value){renderGraph();return;}remember();node[key]=value;changed();});
  $("nodes").addEventListener("input",event=>{if(!numericKeys.includes(event.target.dataset.key))return;const card=event.target.closest(".node");if(!card)return;const node=state.strategy.nodes.find(n=>n.id===card.dataset.id);const value=event.target.value===""?null:Number(event.target.value);const key=event.target.dataset.key;if(node[key]===value)return;remember();node[key]=value;changed(false);});
  $("nodes").addEventListener("click",event=>{const button=event.target.closest("[data-delete]");if(!button)return;remember();state.strategy.nodes=state.strategy.nodes.filter(n=>n.id!==button.dataset.delete);delete state.positions[button.dataset.delete];changed();});
  $("nodes").addEventListener("pointerdown",event=>{const handle=event.target.closest("[data-handle]");if(!handle||event.target.closest("button,input,select"))return;const id=handle.dataset.handle,initial={...state.positions[id]},startX=event.clientX,startY=event.clientY,card=handle.closest(".node");let moved=false;handle.setPointerCapture(event.pointerId);
    const move=e=>{if(!moved&&Math.abs(e.clientX-startX)+Math.abs(e.clientY-startY)>4){remember();moved=true;}if(!moved)return;const pos={x:Math.max(10,initial.x+e.clientX-startX),y:Math.max(10,initial.y+e.clientY-startY)};state.positions[id]=pos;card.style.left=pos.x+"px";card.style.top=pos.y+"px";drawWires();};
    const up=()=>{handle.removeEventListener("pointermove",move);handle.removeEventListener("pointerup",up);handle.removeEventListener("pointercancel",up);renderGraph();};handle.addEventListener("pointermove",move);handle.addEventListener("pointerup",up);handle.addEventListener("pointercancel",up);
  });
  $("strategy-name").addEventListener("input",()=>{remember();state.strategy.name=$("strategy-name").value;changed(false);});
  $("signal").addEventListener("change",()=>{remember();state.strategy.signal=$("signal").value;changed();});
  for(const [id,key] of [["true-weight","when_true"],["false-weight","when_false"]])$(id).addEventListener("input",()=>{remember();state.strategy.allocation[key]=$(id).value===""?null:Number($(id).value);changed(false);});
  $("data-source").addEventListener("change",()=>{updateSymbolInfo();invalidate();validateCurrent();notice("数据源已切换，保留当前日期；请检查覆盖并重新运行。因子实验也将使用该数据源。");});
  for(const id of ["start","end","cost","lag","periods","risk-free"])for(const event of ["input","change"])$(id).addEventListener(event,()=>{updateSymbolInfo();invalidate();validateCurrent();});
  $("symbol").addEventListener("change",()=>changeSymbol());
  $("stock-pool").addEventListener("change",()=>{$("symbol-search").value="";renderSymbolOptions("",true);changeSymbol();});
  $("symbol-search").addEventListener("input",()=>{const previous=$("symbol").value;renderSymbolOptions(previous);if(previous!==$("symbol").value)changeSymbol();else updateSymbolInfo();});
  $("checked-range-btn").addEventListener("click",()=>{const checked=selectedSymbol()?.quality?.checked_range;if(!checked)return;$("start").value=checked.start;$("end").value=checked.end;updateSymbolInfo();invalidate();validateCurrent();notice("已明确改用检查通过的近期区间。区间内未删除记录；回测时仍检查价格与指标窗口。");});
  $("undo-btn").addEventListener("click",()=>{const saved=state.history.pop();if(!saved)return;Object.assign(state,saved);changed();$("undo-btn").disabled=!state.history.length;});
  $("reset-btn").addEventListener("click",()=>{remember();state.strategy=clone(state.bootstrap.default_strategy);state.generationId=null;defaultPositions();changed();notice("已恢复人工 MA5/20 示例。参数仅用于演示，没有自动优化收益。");});
  $("validate-btn").addEventListener("click",validateCurrent);$("save-btn").addEventListener("click",()=>saveWorkspace().catch(e=>notice(e.message,true)));
  $("import-btn").addEventListener("click",()=>$("import-file").click());$("import-file").addEventListener("change",async()=>{try{const file=$("import-file").files[0];if(!file)return;if(file.size>65536)throw new Error("策略文件超过64KB项目上限");await applyWorkspace(JSON.parse(await file.text()));}catch(error){notice("导入未完成："+error.message,true);}finally{$("import-file").value="";}});
  $("generate-btn").addEventListener("click",generate);$("apply-ai-btn").addEventListener("click",()=>{if(!state.candidate||state.candidate.intent_audit?.application_blocked)return;remember();state.strategy=clone(state.candidate.strategy);state.generationId=state.candidate.generation_id;defaultPositions();changed();notice("已应用真实AI候选。可继续编辑，回测报告会区分原始AI策略与人工修改。");});
  $("run-btn").addEventListener("click",run);
  $("financial-btn").addEventListener("click",loadFinancials);
  $("symbol").addEventListener("change",()=>{$("financial-table").hidden=true;$("financial-summary").textContent="已切换标的，请重新查看对应财报。";});
  for(const [id,kind] of [["export-json","report"],["export-csv","records"]])$(id).addEventListener("click",async()=>{if(!state.runId)return;try{await exportArtifact({kind,run_id:state.runId});}catch(error){notice("导出失败："+error.message,true);}});
  $("export-chart").addEventListener("click",async()=>{if(state.result){try{drawChart();await exportArtifact({kind:"chart",run_id:state.runId,image:$("chart").toDataURL("image/png")});}catch(error){notice("净值图导出失败："+error.message,true);}}});
  $("trade-only").addEventListener("change",()=>{state.page=0;renderLedger();});$("previous-page").addEventListener("click",()=>{state.page--;renderLedger();});$("next-page").addEventListener("click",()=>{state.page++;renderLedger();});
  $("chart").addEventListener("mousemove",event=>{if(!state.result||!state.chartGeometry)return;const rect=$("chart").getBoundingClientRect(),g=state.chartGeometry;showChartPoint(Math.round((event.clientX-rect.left-g.pad.left)/(g.w-g.pad.left-g.pad.right)*(state.result.records.length-1)));});$("chart").addEventListener("mouseleave",()=>{$("chart-tooltip").hidden=true;drawChart();});
  $("chart").addEventListener("keydown",event=>{if(!state.result||!state.chartGeometry||!["ArrowLeft","ArrowRight","Home","End"].includes(event.key))return;event.preventDefault();showChartPoint(event.key==="Home"?0:event.key==="End"?state.result.records.length-1:(state.chartFocusIndex??0)+(event.key==="ArrowLeft"?-1:1));});$("chart").addEventListener("blur",()=>{$("chart-tooltip").hidden=true;drawChart();});
  window.addEventListener("resize",()=>{drawChart();drawWires();});
}

async function init(){try{state.bootstrap=await api("/api/bootstrap");state.strategy=clone(state.bootstrap.default_strategy);$("source-count").textContent=`沪深300 · ${state.bootstrap.stock_pool.summary.official_members} 个成员 · ${state.bootstrap.stock_pool.as_of}`;$("ai-status").textContent=state.bootstrap.ai_configured?"AI 密钥已配置 · 尚未调用":"AI 未配置 · 可人工构建";$("generate-btn").disabled=!state.bootstrap.ai_configured;
  document.querySelector('#data-source option[value="public_hfq"]').disabled=!state.bootstrap.data_sources?.public_hfq;
  $("stock-pool").value="csi300";renderSymbolOptions("",true);const first=selectedSymbol();$("start").value=first?.start||"";$("end").value=first?.end||"";updateSymbolInfo();$("cost").value=state.bootstrap.defaults.cost_bps;$("lag").value=state.bootstrap.defaults.lag;$("periods").value=state.bootstrap.defaults.periods_per_year;$("risk-free").value=state.bootstrap.defaults.annual_risk_free_rate*100;renderFundamentals();defaultPositions();bind();renderGraph();await validateCurrent();
  const stored=localStorage.getItem(workspaceStorageKey());if(stored){try{await applyWorkspace(JSON.parse(stored),false);notice("已恢复上次保存的本机策略；请按当前配置重新运行。");}catch{notice("本机保存的旧策略不适用于当前数据，请重新导入或构建。",true);}}
  drawChart();installExperience();installPortfolio();if(typeof installResearch==="function")installResearch();
}catch(error){notice("工作台初始化失败："+error.message+"。请检查本机服务和审计清单。",true);}}
init();

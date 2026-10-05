"use strict";

// 组合检查绑定当前身份与配置版本，任何修改都使旧检查失效。
const portfolioState={check:null,busy:false};
function isPortfolio(){return $("backtest-mode")?.value==="portfolio";}
function portfolioCodes(){return $("portfolio-symbols").value.split(/[,，\s]+/).filter(Boolean);}
function portfolioOptions(){return {max_positions:Number($("portfolio-max").value),position_cap:Number($("portfolio-cap").value)/100,rebalance_every:Number($("portfolio-every").value)};}
function validPortfolioSettings(settings){
  const codes=settings.symbols,p=settings.portfolio;
  if(!Array.isArray(codes)||!codes.length||codes.length>300||new Set(codes).size!==codes.length||codes.some(code=>!state.bootstrap.symbols.some(s=>s.code===code)))throw new Error("组合须选择1–300个已审计且不重复的股票代码");
  if(!settings.start||!settings.end)throw new Error("组合回测须明确开始与结束日期");
  if(!p||Object.keys(p).sort().join(",")!=="max_positions,position_cap,rebalance_every"||!Number.isInteger(p.max_positions)||p.max_positions<1||p.max_positions>300||!Number.isInteger(p.rebalance_every)||p.rebalance_every<1||p.rebalance_every>60||!Number.isFinite(p.position_cap)||p.position_cap<=0||p.position_cap>1)throw new Error("最大持股数、单股上限或调仓间隔无效");
}
function portfolioPayload(){const {symbol,mode,...payload}=config();return {strategy:state.strategy,...payload,generation_id:state.generationId};}
function portfolioLabels(combo){
  if($("benchmark-metric-label").firstChild)$("benchmark-metric-label").firstChild.nodeValue=combo?"股票池等权买入持有收益率":"买入并持有收益率";
  if($("chart-baseline-label").lastChild)$("chart-baseline-label").lastChild.textContent=combo?"股票池等权买入持有":"买入持有";
  if($("trade-count-label").firstChild)$("trade-count-label").firstChild.nodeValue=combo?"逐股成交记录数":"调仓记录数";
}
function clearPortfolioCheck(){portfolioState.check=null;$("portfolio-check-result").textContent="修改集合、策略或配置后需重新检查。";$("portfolio-use-eligible").hidden=true;$("portfolio-detail").hidden=true;$("portfolio-day-result").textContent="";$("portfolio-day").innerHTML="";$("experiment-btn").disabled=isPortfolio();portfolioLabels(isPortfolio());}
function updatePortfolioMode(){
  const enabled=isPortfolio();$("portfolio-config").hidden=!enabled;$("single-symbol-config").hidden=enabled;
  portfolioLabels(enabled);
  $("experiment-btn").disabled=enabled;$("portfolio-experiment-note").hidden=!enabled;
  $("allocation-mode-note").textContent=enabled?"组合模式：条件为真持仓比例是整个账户的股票总预算，按入选股票等权且受单股上限约束；否则比例必须为0。调仓日不再入选即退出，穿越条件只在发生当日为真。":"单股模式：比例是当前一只股票的目标持仓。";
  if(enabled)$("pool-info").textContent="选择明确股票集合后逐日扫描条件，共享一份现金。当前沪深300名单回看历史，不是历史逐日成分池。";
}
async function checkPortfolio(){
  if(portfolioState.busy||!isPortfolio())return;
  const rev=state.revision,identity=identityRevision;portfolioState.busy=true;$("portfolio-check-btn").disabled=true;$("portfolio-check-btn").textContent="正在逐股检查真实行情…";$("run-btn").disabled=true;
  try{
    const payload=portfolioPayload();validPortfolioSettings(payload);
    const check=await api("/api/portfolio/preflight",payload);
    if(rev!==state.revision||identity!==identityRevision)return;
    portfolioState.check=check;
    $("portfolio-check-result").innerHTML=`<p>${escapeHtml(check.status)} · 请求 ${check.requested} 股，通过 ${check.eligible_symbols.length} 股，共同日历 ${check.calendar_records} 条。${escapeHtml(check.scope)}</p>${researchTable(["股票","状态","记录","原因"],check.items.map(item=>[item.name+" · "+item.code,item.eligible?"通过":"阻断",item.rows??"—",item.reason||"质量与日历通过"]))}`;
    $("portfolio-use-eligible").hidden=check.status==="PASS"||!check.eligible_symbols.length;
    await validateCurrent();
  }catch(error){if(rev===state.revision&&identity===identityRevision){portfolioState.check=null;$("portfolio-check-result").textContent="检查未完成："+error.message;notice(error.message,true);}}
  finally{portfolioState.busy=false;$("portfolio-check-btn").disabled=false;$("portfolio-check-btn").textContent="检查股票池数据";$("run-btn").disabled=state.runBusy||!state.valid||!canRunSelected();}
}
function applyPortfolioSettings(settings){
  $("backtest-mode").value=settings.mode||"single";
  if(settings.mode==="portfolio"){$("portfolio-symbols").value=settings.symbols.join(", ");$("portfolio-max").value=settings.portfolio.max_positions;$("portfolio-cap").value=settings.portfolio.position_cap*100;$("portfolio-every").value=settings.portfolio.rebalance_every;}
  updatePortfolioMode();
}
function portfolioDayMarkup(report,day){
  const row=report.records.find(record=>record.date===day);if(!row)return "该日期不在本次共同日历中。";
  const name=code=>state.bootstrap.symbols.find(s=>s.code===code)?.name||code;
  return `<p>${escapeHtml(day)} · ${row.rebalance?"调仓日":"保持原股数"} · 信号来源 ${escapeHtml(row.executed_signal_date||"预热或非调仓日")} · 现金 ${row.cash.toFixed(6)}（归一化单位）。上限约束目标仓位，非调仓日期实际权重可随价格漂移。</p><p>已滞后条件命中：${escapeHtml(row.matched_symbols.join(", ")||"无")}；本次选股：${escapeHtml(row.selected_symbols.join(", ")||"无")}；超出持股数限制：${escapeHtml(row.capacity_excluded.join(", ")||"无")}。</p>${researchTable(["持仓股票","股数（分数单位）","收盘价","实际权重","目标权重"],row.holdings.map(h=>[name(h.symbol)+" · "+h.symbol,h.units.toFixed(6),h.price.toFixed(2),percent(h.weight),percent(h.target_weight)]))}${researchTable(["交易股票","方向","成交价","交易金额","费用"],row.trades.map(t=>[name(t.symbol)+" · "+t.symbol,t.side,t.price.toFixed(2),t.notional.toFixed(6),t.fee.toFixed(6)]))}`;
}
function renderPortfolioDay(day){if(state.result?.kind==="portfolio")$("portfolio-day-result").innerHTML=portfolioDayMarkup(state.result,day);}
function renderPortfolioResult(){
  const report=state.result;$("portfolio-detail").hidden=report?.kind!=="portfolio";
  portfolioLabels(report?.kind==="portfolio");
  $("experiment-btn").disabled=report?.kind==="portfolio";
  $("portfolio-experiment-note").hidden=report?.kind!=="portfolio";
  if(report?.kind!=="portfolio")return;
  $("portfolio-day").innerHTML=report.records.map(row=>`<option value="${row.date}">${row.date}${row.trades.length?" · "+row.trades.length+"笔交易":""}</option>`).join("");
  $("portfolio-day").value=report.records.find(row=>row.trades.length)?.date||report.metrics.end;renderPortfolioDay($("portfolio-day").value);
}
function installPortfolio(){
  for(const [id,term] of [["data-source","data_source"],["backtest-mode","portfolio"],["portfolio-cap","position_cap"],["portfolio-every","rebalance_every"]])document.querySelector(`[for="${id}"]`)?.insertAdjacentHTML("beforeend",termHelp(term));
  $("backtest-mode").addEventListener("change",()=>{updatePortfolioMode();invalidate();validateCurrent();});
  for(const id of ["portfolio-symbols","portfolio-max","portfolio-cap","portfolio-every"])for(const event of ["input","change"])$(id).addEventListener(event,()=>{invalidate();validateCurrent();});
  $("portfolio-all-btn").addEventListener("click",()=>{$("portfolio-symbols").value=state.bootstrap.symbols.filter(s=>(s.pool||"demo")===$("stock-pool").value).map(s=>s.code).join(", ");invalidate();validateCurrent();notice("已明确选择当前股票池全部标的，请检查区间数据；不会自动剔除失败股票。");});
  $("portfolio-check-btn").addEventListener("click",checkPortfolio);
  $("portfolio-use-eligible").addEventListener("click",async()=>{const checked=portfolioState.check;if(!checked)return;$("portfolio-symbols").value=checked.eligible_symbols.join(", ");invalidate();notice(`已按你的操作明确将集合改为 ${checked.eligible_symbols.length} 股，排除 ${checked.requested-checked.eligible_symbols.length} 股；正在重新检查。`);await checkPortfolio();});
  $("portfolio-example-btn").addEventListener("click",async()=>{try{await applyWorkspace({format:"finblocks-workspace",version:1,strategy:clone(state.bootstrap.default_strategy),config:{symbol:"sh688981",start:"2026-06-01",end:"2026-08-19",cost_bps:10,lag:1,periods_per_year:252,annual_risk_free_rate:0,mode:"portfolio",symbols:["sh688047","sh688506","sh688521","sh688981"],portfolio:{max_positions:4,position_cap:0.25,rebalance_every:1}}});notice("已载入人工MA5/20四股示例，替换策略与配置；参数为演示假设，未按收益择优。请检查股票池数据后运行。");}catch(error){notice(error.message,true);}});
  $("portfolio-day").addEventListener("change",()=>renderPortfolioDay($("portfolio-day").value));updatePortfolioMode();
}

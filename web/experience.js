"use strict";

// 主界面保留专业术语，解释按需打开；数值定义以实际内核为准。
const TERMS = {
  pool:{title:"股票池",text:"本次研究可选择的股票集合。沪深300入口采用标注日期的官方成分名单，不代表历史每天的成分，也不代表同时买入300只股票。"},
  symbol:{title:"研究标的",text:"当前检验策略的一只股票。切换标的后，日期和结果需要重新确认。"},
  data_source:{title:"后复权与公开快照",text:"后复权调整价格序列，处理权益事件带来的断点。本平台公开价格是研究单位，不是真实订单报价；供应商回退day的标的会单独检查。获取日期和单位证据保存在报告中，前收盘由同一快照上一条收盘派生，原始ZIP未修改。换源会清除旧结果；基本面历史披露时点仍待解决。"},
  portfolio:{title:"股票池组合回测",text:"逐股检验同一条件，所有持仓共享一个现金账户。满足条件的股票按预算等权分配，超额候选按代码升序截取；调仓日不再入选的股票卖出。当前成分回看历史存在选样偏差。"},
  position_cap:{title:"单股目标仓位上限",text:"调仓时一只股票占扣费后组合净值的最大目标比例；预算无法用完时留现金。非调仓日价格变动会造成实际权重漂移，上限不是自动止损。"},
  rebalance_every:{title:"调仓间隔",text:"按共同日历记录计数，1表示每条记录调仓，5不是固定自然周。调仓使用滞后信号，间隔内保持股数；条件失效会等到下一调仓日才退出。"},
  field:{title:"OHLCV 行情字段",text:"O、H、L、C分别是开盘价、最高价、最低价、收盘价；V是成交量。只使用本地文件实际存在的字段。"},
  ma:{title:"移动平均线 MA",text:"指定窗口内行情数值的算术平均，用来观察近期水平。窗口按真实记录计数；MA5/20是演示参数，不是最优参数。"},
  ema:{title:"指数移动平均线 EMA",text:"对近期记录赋予更高权重的移动平均。本项目以窗口内简单平均初始化，随后按2/(窗口+1)递推。"},
  rsi:{title:"相对强弱指数 RSI",text:"比较一段记录中上涨与下跌幅度的指标。本项目采用Wilder平滑；数值高低本身不是确定的买卖结论。"},
  bollinger:{title:"布林带 Bollinger Bands",text:"中轨为移动平均线，上下轨为中轨加减标准差倍数。本项目使用总体标准差，窗口和倍数可编辑。"},
  macd:{title:"指数平滑异同移动平均线 MACD",text:"DIF为快EMA减慢EMA，DEA为DIF的EMA。本项目柱值是DIF−DEA，没有乘2；请按这一定义理解图形和条件。"},
  condition:{title:"策略条件与交叉信号",text:"“严格高于”是持续状态；“向上穿越”只在上一条不高于、当前严格高于时为真。两者不能互换，使用的数据必须在信号时点可得。"},
  logic:{title:"逻辑组合 AND / OR",text:"AND要求两个条件同时为真，OR要求至少一个条件为真。组合的每个输入都应是布尔条件。"},
  const:{title:"数值常量",text:"固定的比较阈值或数值输入。填写常量不会自动优化策略，需要说明其研究用途。"},
  allocation:{title:"持仓比例",text:"分数持仓模型中资产占净值的目标比例，0表示现金、1表示全部资产。当前模型不等同交易所整手撮合。"},
  cost:{title:"交易成本与基点",text:"1基点等于0.01%。本项目成本按调仓成交额收取；10基点是研究情景，不是真实券商费用。"},
  lag:{title:"信号执行滞后",text:"在本条收盘生成信号，经过指定条数的真实记录后，在收盘执行。不是即时成交，也不保证真实市场可成交。"},
  periods:{title:"年化周期数",text:"把每条相邻净值收益视为一个周期，用指定的每年周期数年化。默认252是项目假设，并非数据真实交易日历。"},
  riskfree:{title:"年化无风险利率",text:"夏普计算中的比较利率。页面输入百分比，内核转换为每周期复合利率；默认0%是可编辑研究假设。"},
  totalreturn:{title:"累计收益率",text:"期末净值相对期初资本的变化比例，尚未年化。负数表示这段历史样本出现损失。"},
  benchmark:{title:"买入并持有基准",text:"在同一股票、区间和成本假设下买入并持有，作为策略比较对象；它不是沪深300指数。"},
  drawdown:{title:"最大回撤",text:"净值从过去最高点下跌到后续低点的最深幅度。本项目显示负数，例如−10%表示曾较历史峰值下降10%，不等同最终亏损。"},
  trades:{title:"调仓记录数",text:"回测中真实持仓发生调整的记录数量；不等于盈利交易次数或往返交易次数。"},
  sharpe:{title:"夏普比率",text:"平均每周期超额收益除以样本标准差，再乘年化周期数的平方根。用于比较收益相对波动；标准差为零或样本不足时没有定义。"},
  volatility:{title:"年化波动率",text:"相邻净值收益的样本标准差乘年化周期数的平方根。反映波动幅度，不代表全部风险。"},
  annualreturn:{title:"年化收益率",text:"根据首末净值和实际收益周期数，把历史复合增长换算到一年。短样本年化可能非常不稳定。"},
  equity:{title:"归一化净值",text:"把初始资本设为1后的资产价值曲线，用于在同一尺度比较策略与基准；不是实际账户余额。"},
  pit:{title:"PIT（Point-in-Time，时点数据）",text:"在每个历史时点只使用当时已公开且可得的数据。后来修订的财报不能倒填过去；当前基本面未达到这一核验门槛。"},
  pe:{title:"市盈率 PE",text:"价格与每股收益的比率。本地历史利润单位、口径及修订可得时点未核验，因此当前不能用于历史策略回测。"},
  pb:{title:"市净率 PB",text:"价格与每股净资产的比率。当前历史净资产单位、版本及可得时点未核验，不能用于回测。"},
  roe:{title:"净资产收益率 ROE",text:"衡量利润相对权益的指标，具体定义取决于利润和权益口径；当前原字段尚不能可靠构成历史ROE。"},
  growth:{title:"营收增长与利润增长",text:"需要对齐相同财报期间、累计或单季口径、单位及历史版本。当前资料尚未完成这些核验，不能补造增长率。"}
};

function termHelp(key){
  return TERMS[key]?`<button type="button" class="term-help" data-term="${key}" aria-label="查看${escapeHtml(TERMS[key].title)}解释" aria-haspopup="dialog">?</button>`:"";
}

function showTerm(key){
  const entry=TERMS[key];if(!entry)return;
  $("term-title").textContent=entry.title;$("term-explanation").textContent=entry.text;
  if(!$("term-dialog").open)$("term-dialog").showModal();
}

const TOUR_STEPS = [
  {target:".config-panel",title:"1. 选择股票池与研究标的",text:"先选股票池，再按名称或代码找到研究标的。确认名单日期、本地行情区间和数据质量；价格异常时，可明确选择已检查的近期区间。"},
  {target:".builder",title:"2. 检查策略条件与持仓比例",text:"画布中的积木定义实际执行规则。移动平均线的窗口、持续高于与上穿事件都有不同含义；点击问号查看解释，再校验策略。"},
  {target:".ai-panel",title:"3. 可选使用 AI 策略助手",text:"描述条件和参数，取得候选后先核对并明确应用，再执行回测。AI未配置时可人工编辑；引导不会自动调用模型或修改策略。"},
  {target:".config-panel",title:"4. 配置并执行回测",text:"确认开始/结束日期、交易成本、执行滞后和年化假设，再点击运行。指标需要预热记录；数据或窗口不符合要求时会显示原因。"},
  {target:".results",title:"5. 解读收益、风险与基准",text:"比较累计收益率、买入并持有基准、最大回撤和夏普比率。查看回测账目及来源假设后可导出报告；历史表现不是未来收益保证。"}
];
let tourIndex=-1,authMode="login",authBusy=false,identityRevision=0;
function onboardingKey(){return "finblocks-onboarding-v1-"+(state.bootstrap.user?.id||"guest");}
function rememberGuide(choice){try{localStorage.setItem(onboardingKey(),choice);}catch{/* 存储不可用时本次仍可使用引导。 */}}
function stopTour(choice="skipped"){
  if(tourIndex>=0)rememberGuide(choice);
  document.querySelectorAll(".guide-highlight").forEach(e=>e.classList.remove("guide-highlight"));
  $("tour-panel").hidden=true;tourIndex=-1;
}
function renderTour(){
  document.querySelectorAll(".guide-highlight").forEach(e=>e.classList.remove("guide-highlight"));
  const step=TOUR_STEPS[tourIndex],target=document.querySelector(step.target);
  $("tour-title").textContent=step.title;$("tour-text").textContent=step.text;$("tour-count").textContent=`${tourIndex+1} / ${TOUR_STEPS.length}`;
  $("tour-prev").disabled=tourIndex===0;$("tour-next").textContent=tourIndex===TOUR_STEPS.length-1?"完成引导":"下一步";
  $("tour-panel").hidden=false;
  if(target){target.classList.add("guide-highlight");target.scrollIntoView({block:"start",behavior:"instant"});}
  $("tour-next").focus({preventScroll:true});
}
function startTour(){if($("welcome-dialog").open)$("welcome-dialog").close();tourIndex=0;rememberGuide("started");renderTour();}
function offerGuide(){let choice;try{choice=localStorage.getItem(onboardingKey());}catch{choice=null;}if(!choice&&!$("welcome-dialog").open)$("welcome-dialog").showModal();}

function renderAccount(){
  const user=state.bootstrap.user;$("account-status").textContent=user?(state.bootstrap.online?"组员账号 · ":"测试账号 · ")+user.username:"游客";
  $("account-btn").textContent=user?"切换账号":"注册 / 登录";$("logout-btn").hidden=!user;
}
function openAccount(mode="login"){
  stopTour();authMode=mode;$("auth-title").textContent=mode==="register"?"注册测试账号":"登录测试账号";
  $("auth-confirm-field").hidden=mode!=="register";$("auth-confirm").required=mode==="register";
  $("auth-invite-field").hidden=!(state.bootstrap.online&&mode==="register");$("auth-invite").required=state.bootstrap.online&&mode==="register";
  if(state.bootstrap.online)$("auth-description").textContent="组员使用邀请码注册，只需用户名和密码，不绑定手机号或邮箱。账号与研究历史保存在服务端，金融数据与AI功能须登录后使用。";
  $("auth-password").autocomplete=mode==="register"?"new-password":"current-password";
  $("auth-submit").textContent=mode==="register"?"注册并登录":"登录";
  $("auth-switch").textContent=mode==="register"?"已有账号，去登录":"没有账号，去注册";
  $("auth-error").textContent="";$("auth-password").value="";$("auth-confirm").value="";
  if(!$("auth-dialog").open)$("auth-dialog").showModal();
  $("auth-username").focus();
}

async function restoreIdentityWorkspace(){
  const stored=localStorage.getItem(workspaceStorageKey());
  if(stored){try{await applyWorkspace(JSON.parse(stored),false);return;}catch{notice("该账号保存的策略未能恢复，请检查后重新导入。",true);}}
  state.strategy=clone(state.bootstrap.default_strategy);state.generationId=null;state.candidate=null;state.history=[];$("undo-btn").disabled=true;
  $("data-source").value="original";$("backtest-mode").value="single";$("portfolio-symbols").value="";$("portfolio-max").value=5;$("portfolio-cap").value=25;$("portfolio-every").value=1;updatePortfolioMode();
  $("stock-pool").value="csi300";$("symbol-search").value="";renderSymbolOptions("",true);const first=selectedSymbol();
  $("start").value=first?.start||"";$("end").value=first?.end||"";
  $("cost").value=state.bootstrap.defaults.cost_bps;$("lag").value=state.bootstrap.defaults.lag;
  $("periods").value=state.bootstrap.defaults.periods_per_year;$("risk-free").value=state.bootstrap.defaults.annual_risk_free_rate*100;
  defaultPositions();updateSymbolInfo();changed();
}
async function changeIdentity(user){
  identityRevision++;state.bootstrap.user=user;state.candidate=null;state.generationId=null;state.history=[];$("undo-btn").disabled=true;
  $("ai-status").textContent=state.bootstrap.ai_configured?"AI 密钥已配置 · 尚未调用":"AI 未配置 · 可人工构建";
  $("ai-candidate").hidden=true;$("financial-table").hidden=true;$("financial-summary").textContent="账户已切换，请重新查看财报。";
  invalidate();state.strategy=null;state.positions={};renderAccount();await restoreIdentityWorkspace();offerGuide();
}
async function submitAccount(event){
  event.preventDefault();if(authBusy)return;
  const password=$("auth-password").value;
  if(authMode==="register"&&password!==$("auth-confirm").value){$("auth-error").textContent="两次密码不一致，请重新确认。";return;}
  authBusy=true;$("auth-submit").disabled=true;$("auth-close").disabled=true;$("auth-switch").disabled=true;$("auth-error").textContent="";
  const headers=state.bootstrap.online&&authMode==="register"?{"X-FinBlocks-Invite":$("auth-invite").value.trim()}:{};
  try{const result=await api("/api/auth/"+authMode,{username:$("auth-username").value,password},headers);
    $("auth-invite").value="";
    $("auth-password").value="";$("auth-confirm").value="";$("auth-dialog").close();await changeIdentity(result.user);notice("已登录测试账号。策略快照与游客或其他账号分开保存。");
  }catch(error){$("auth-error").textContent=error.message;}
  finally{authBusy=false;$("auth-submit").disabled=false;$("auth-close").disabled=false;$("auth-switch").disabled=false;}
}

function installExperience(){
  // 库中帮助按钮与添加按钮并列，避免嵌套按钮或点击问号误添加积木。
  document.querySelectorAll(".block-add").forEach(button=>{const key={gt:"condition",and:"logic"}[button.dataset.add]||button.dataset.add;
    if(!TERMS[key])return;const wrapper=document.createElement("div");wrapper.className="library-item";button.before(wrapper);wrapper.append(button);wrapper.insertAdjacentHTML("beforeend",termHelp(key));
  });
  const labels={"stock-pool":"pool",symbol:"symbol",cost:"cost",lag:"lag",periods:"periods","risk-free":"riskfree","true-weight":"allocation","false-weight":"allocation",signal:"condition"};
  for(const [id,key] of Object.entries(labels)){const label=document.querySelector(`label[for="${id}"]`);if(!label)continue;
    const wrapper=document.createElement("div");wrapper.className="label-with-help";label.before(wrapper);wrapper.append(label);wrapper.insertAdjacentHTML("beforeend",termHelp(key));
  }
  const metrics={"metric-return":"totalreturn","metric-benchmark":"benchmark","metric-drawdown":"drawdown","metric-trades":"trades","metric-sharpe":"sharpe","metric-benchmark-sharpe":"sharpe","metric-volatility":"volatility","metric-annual-return":"annualreturn"};
  for(const [id,key] of Object.entries(metrics))$(id).previousElementSibling.insertAdjacentHTML("beforeend",termHelp(key));
  document.querySelector(".chart-heading b").insertAdjacentHTML("beforeend",termHelp("equity"));
  document.querySelector(".fundamental-panel h2").insertAdjacentHTML("beforeend",termHelp("pit"));
  document.querySelectorAll(".fundamental-status span").forEach((span,index)=>span.insertAdjacentHTML("beforeend",termHelp(["pe","pb","roe","growth","growth"][index])));
  document.addEventListener("click",event=>{const help=event.target.closest("[data-term]");if(help){event.preventDefault();showTerm(help.dataset.term);}});
  $("term-close").addEventListener("click",()=>$("term-dialog").close());
  $("guide-btn").addEventListener("click",startTour);$("welcome-start").addEventListener("click",startTour);
  $("welcome-skip").addEventListener("click",()=>{rememberGuide("skipped");$("welcome-dialog").close();});
  $("welcome-dialog").addEventListener("cancel",()=>rememberGuide("skipped"));
  $("tour-prev").addEventListener("click",()=>{if(tourIndex>0){tourIndex--;renderTour();}});
  $("tour-next").addEventListener("click",()=>{if(tourIndex===TOUR_STEPS.length-1){stopTour("completed");$("guide-btn").focus();}else{tourIndex++;renderTour();}});
  $("tour-exit").addEventListener("click",()=>{stopTour();$("guide-btn").focus();});
  document.addEventListener("keydown",event=>{if(event.key==="Escape"&&tourIndex>=0&&!$("term-dialog").open&&!$("auth-dialog").open){stopTour();$("guide-btn").focus();}});
  $("account-btn").addEventListener("click",()=>openAccount());$("auth-close").addEventListener("click",()=>{$("auth-dialog").close();$("auth-password").value="";$("auth-confirm").value="";});
  $("auth-switch").addEventListener("click",()=>openAccount(authMode==="login"?"register":"login"));
  $("auth-form").addEventListener("submit",submitAccount);
  $("auth-dialog").addEventListener("cancel",event=>{if(authBusy)event.preventDefault();else{$("auth-password").value="";$("auth-confirm").value="";}});
  $("logout-btn").addEventListener("click",async()=>{if(authBusy)return;authBusy=true;$("logout-btn").disabled=true;
    try{await api("/api/auth/logout",{});stopTour();await changeIdentity(null);notice("已退出测试账号，恢复游客工作台。");}
    catch(error){notice("退出未完成："+error.message,true);}finally{authBusy=false;$("logout-btn").disabled=false;}
  });
  // 锚点导航保留原生链接行为；滚动时标出当前研究区域。
  const navigation=Array.from(document.querySelectorAll('.research-nav a[href^="#"]'));
  const markSection=id=>navigation.forEach(link=>{if(link.getAttribute("href")==="#"+id)link.setAttribute("aria-current","location");else link.removeAttribute("aria-current");});
  navigation.forEach(link=>link.addEventListener("click",()=>markSection(link.getAttribute("href").slice(1))));
  if(window.IntersectionObserver){const observer=new IntersectionObserver(()=>{const sections=navigation.map(link=>$(link.getAttribute("href").slice(1))).filter(Boolean);const current=sections.filter(section=>section.getBoundingClientRect().top<=140).at(-1);if(current)markSection(current.id);},{rootMargin:"-80px 0px -65% 0px"});navigation.forEach(link=>{const section=$(link.getAttribute("href").slice(1));if(section)observer.observe(section);});}
  renderAccount();offerGuide();
}

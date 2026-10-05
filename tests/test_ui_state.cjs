// 直接加载工作台函数，用受控请求延迟检查异步状态；响应占位对象不充当金融数据。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise, resolve};
}

function harness() {
  const elements = new Map();
  const storage = new Map();
  const context = {
    console, setTimeout, clearTimeout,
    document: {getElementById(id) {
      if (!elements.has(id)) elements.set(id, {value: "", disabled: false, textContent: ""});
      return elements.get(id);
    }},
    localStorage: {setItem: (key, value) => storage.set(key, value)},
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(root, "web/experience.js"), "utf8"), context);
  vm.runInContext(fs.readFileSync(path.join(root,"web/portfolio.js"),"utf8"),context);
  const source = fs.readFileSync(path.join(root, "web/app.js"), "utf8");
  vm.runInContext(source.replace(/\ninit\(\);\s*$/, ""), context);
  const state = vm.runInContext("state", context);
  state.strategy = JSON.parse(fs.readFileSync(path.join(root, "examples/ma_strategy.json"), "utf8"));
  state.positions = {price: {x: 20, y: 130}};
  state.bootstrap = {symbols: [{code: "sh600455"}]};
  state.valid = true;
  for (const [id, value] of Object.entries({symbol: "sh600455", start: "2024-01-01", end: "2026-08-19", cost: "10", lag: "1", periods: "252", "risk-free": "0"})) {
    context.document.getElementById(id).value = value;
  }
  context.validateCurrent = async () => true;
  const notices = [];
  context.notice = message => notices.push(message);
  return {context, state, storage, notices};
}

test("公开数据源同时进入回测与因子协议，旧配置仍使用原始数据", () => {
  const {context}=harness();
  assert.equal(Object.hasOwn(context.config(),"data_source"),false);
  context.document.getElementById("data-source").value="public_hfq";
  assert.equal(context.config().data_source,"public_hfq");
  vm.runInContext(fs.readFileSync(path.join(root,"web/research.js"),"utf8"),context);
  context.document.getElementById("factor-protocol-confirm").checked=true;
  context.document.getElementById("factor-start").value="2026-06-01";
  context.document.getElementById("factor-end").value="2026-08-19";
  assert.equal(context.factorRequest().data_source,"public_hfq");
});

test("切换数据源后清除旧股票池检查、回测及因子冻结状态", () => {
  const {context,state}=harness();
  vm.runInContext(fs.readFileSync(path.join(root,"web/research.js"),"utf8"),context);
  vm.runInContext('portfolioState.check={status:"PASS"};researchState.validation={research_id:"old"};',context);
  state.result={kind:"portfolio"};state.runId="old";
  context.document.getElementById("data-source").value="public_hfq";
  context.drawChart=()=>{};
  context.invalidate();
  assert.equal(vm.runInContext("portfolioState.check",context),null);
  assert.equal(vm.runInContext("researchState.validation",context),null);
  assert.equal(state.result,null);
});

test("未知数据源导入在修改当前策略之前被拒绝", async () => {
  const {context,state}=harness();context.api=async()=>({valid:true});
  const original=JSON.stringify(state.strategy);
  await assert.rejects(context.applyWorkspace({format:"finblocks-workspace",version:1,strategy:state.strategy,
    config:{...context.config(),data_source:"unknown_vendor"}}),/数据源不受支持/);
  assert.equal(JSON.stringify(state.strategy),original);
});

test("研究请求期间账号变化会丢弃响应", async () => {
  const {context} = harness();
  vm.runInContext(fs.readFileSync(path.join(root,"web/research.js"),"utf8"),context);
  const pending=deferred();context.pendingResearch=pending.promise;
  const task=vm.runInContext('researchAction("audit-btn",async current=>{await pendingResearch;if(current())$("audit-result").textContent="不应应用";})',context);
  vm.runInContext("identityRevision++",context);pending.resolve();await task;
  assert.notEqual(context.document.getElementById("audit-result").textContent,"不应应用");
});

test("组合回测发送完整集合与共享资金设置，不混入单股参数", async () => {
  const {context,state}=harness();
  for(const [id,value] of Object.entries({"backtest-mode":"portfolio","portfolio-symbols":"sh600455","portfolio-max":"5","portfolio-cap":"25","portfolio-every":"1"}))context.document.getElementById(id).value=value;
  vm.runInContext('portfolioState.check={status:"PASS"}',context);
  let request;context.api=async(path,body)=>{request={path,body};return {run_id:"scope_test",report:{kind:"portfolio"}};};context.renderResults=()=>{};
  await context.run();
  assert.equal(request.path,"/api/portfolio/backtest");
  assert.deepEqual(Array.from(request.body.symbols),["sh600455"]);
  assert.equal(request.body.portfolio.position_cap,.25);
  assert.equal(request.body.portfolio.max_positions,5);
  assert.equal(Object.hasOwn(request.body,"symbol"),false);
  assert.equal(Object.hasOwn(request.body,"mode"),false);
  assert.equal(state.runId,"scope_test");
});

test("股票池检查期间修改集合或切换身份不会应用旧检查", async () => {
  for(const change of ["revision","identity"]){
    const {context,state}=harness();
    for(const [id,value] of Object.entries({"backtest-mode":"portfolio","portfolio-symbols":"sh600455","portfolio-max":"5","portfolio-cap":"25","portfolio-every":"1"}))context.document.getElementById(id).value=value;
    const pending=deferred();context.api=()=>pending.promise;
    const checking=context.checkPortfolio();
    if(change==="revision")state.revision++;else vm.runInContext("identityRevision++",context);
    pending.resolve({status:"PASS",eligible_symbols:["sh600455"]});await checking;
    assert.equal(vm.runInContext("portfolioState.check",context),null);
    assert.equal(context.canRunSelected(),false);
    assert.equal(context.document.getElementById("run-btn").disabled,true);
  }
});

test("无效组合导入不会替换原策略，失效时清除旧持仓详情", async () => {
  const {context,state}=harness();const original=JSON.stringify(state.strategy);context.api=async()=>({valid:true});
  const candidate={format:"finblocks-workspace",version:1,strategy:JSON.parse(original),config:{symbol:"sh600455",start:"2024-01-01",end:"2026-08-19",cost_bps:10,lag:1,mode:"portfolio",symbols:["sh600455","sh600455"],portfolio:{max_positions:2,position_cap:.5,rebalance_every:1}}};
  candidate.strategy.name="不能应用";
  await assert.rejects(context.applyWorkspace(candidate),/不重复/);
  assert.equal(JSON.stringify(state.strategy),original);
  vm.runInContext('portfolioState.check={status:"PASS"}',context);context.document.getElementById("portfolio-day-result").textContent="旧身份持仓";
  context.renderLedger=()=>{};context.drawChart=()=>{};context.invalidate();
  assert.equal(vm.runInContext("portfolioState.check",context),null);
  assert.equal(context.document.getElementById("portfolio-day-result").textContent,"");
  assert.equal(context.document.getElementById("portfolio-detail").hidden,true);
});

test("部分股票检查失败不会静默删股，通过检查才允许组合运行", async () => {
  const {context,state}=harness();vm.runInContext(fs.readFileSync(path.join(root,"web/research.js"),"utf8"),context);
  state.bootstrap.symbols.push({code:"sh600000"});
  for(const [id,value] of Object.entries({"backtest-mode":"portfolio","portfolio-symbols":"sh600455, sh600000","portfolio-max":"5","portfolio-cap":"25","portfolio-every":"1"}))context.document.getElementById(id).value=value;
  const items=[{code:"sh600455",name:"控制样例A",eligible:true,rows:30},{code:"sh600000",name:"控制样例B",eligible:false,reason:"日历缺失"}];
  context.api=async()=>({status:"BLOCKED",requested:2,eligible_symbols:["sh600455"],calendar_records:30,items,scope:"测试检查状态，不是金融回测结果"});
  await context.checkPortfolio();
  assert.equal(context.document.getElementById("portfolio-symbols").value,"sh600455, sh600000");
  assert.equal(context.canRunSelected(),false);
  assert.equal(context.document.getElementById("portfolio-use-eligible").hidden,false);
  context.api=async()=>({status:"PASS",requested:2,eligible_symbols:["sh600455","sh600000"],calendar_records:30,items,scope:"测试检查状态"});
  await context.checkPortfolio();assert.equal(context.canRunSelected(),true);assert.equal(context.document.getElementById("run-btn").disabled,false);
});

test("研究表单修改会丢弃旧研究结果", async () => {
  const {context} = harness();
  vm.runInContext(fs.readFileSync(path.join(root,"web/research.js"),"utf8"),context);
  const pending=deferred();context.pendingResearch=pending.promise;
  const task=vm.runInContext('researchAction("audit-btn",async current=>{await pendingResearch;if(current())$("audit-result").textContent="旧契约";})',context);
  vm.runInContext("researchState.version++",context);pending.resolve();await task;
  assert.notEqual(context.document.getElementById("audit-result").textContent,"旧契约");
});

test("账号变化清理研究公式、候选和确认状态", () => {
  const {context}=harness();
  vm.runInContext(fs.readFileSync(path.join(root,"web/research.js"),"utf8"),context);
  context.document.getElementById("factor-expression").value="旧账号私有公式";
  context.document.getElementById("factor-protocol-confirm").checked=true;
  vm.runInContext("researchState.identity=identityRevision;identityRevision++;resetResearch()",context);
  assert.equal(context.document.getElementById("factor-expression").value,"");
  assert.equal(context.document.getElementById("factor-protocol-confirm").checked,false);
});

test("相同研究按钮不重复执行并发请求", async () => {
  const {context} = harness();
  vm.runInContext(fs.readFileSync(path.join(root,"web/research.js"),"utf8"),context);
  const pending=deferred();context.pendingResearch=pending.promise;
  vm.runInContext("var researchCalls=0",context);
  const first=vm.runInContext('researchAction("audit-btn",async()=>{researchCalls++;await pendingResearch;})',context);
  await vm.runInContext('researchAction("audit-btn",async()=>{researchCalls++;})',context);
  pending.resolve();await first;assert.equal(vm.runInContext("researchCalls",context),1);
});

test("因子算术策略可生成真实条件摘要", () => {
  const {context}=harness();
  const strategy={version:1,name:"算术摘要专测",nodes:[{id:"p",op:"field",field:"close"},{id:"v",op:"const",value:2},{id:"f",op:"div",left:"p",right:"v"},{id:"s",op:"gt",left:"f",right:"v"}],signal:"s",allocation:{when_true:1,when_false:0}};
  assert.match(context.describeStrategy(strategy),/除法/);
  assert.match(context.describeStrategy(strategy),/持仓 100%/);
});

test("搜索全成分池不会静默切换股票，空匹配不能运行", () => {
  const {context, state} = harness();
  const manifest = JSON.parse(fs.readFileSync(path.join(root, "data/csi300_manifest.json"), "utf8"));
  state.bootstrap.symbols = manifest.members.map(item => ({...item.source, ...item}));
  context.document.getElementById("stock-pool").value = "csi300";
  context.document.getElementById("symbol-search").value = "";
  assert.equal(context.filteredSymbols().length, 300);
  context.renderSymbolOptions("sz000001");
  assert.equal(context.document.getElementById("symbol").value, "sz000001");
  context.document.getElementById("symbol-search").value = "贵州茅台";
  context.renderSymbolOptions("sz000001");
  assert.equal(context.filteredSymbols().length, 1);
  assert.equal(context.document.getElementById("symbol").value, "");
  assert.equal(context.canRunSelected(), false);
  context.document.getElementById("symbol-search").value = "不存在的股票名称";
  context.renderSymbolOptions();
  assert.equal(context.document.getElementById("symbol").disabled, true);
  assert.match(context.document.getElementById("search-status").textContent, /没有找到/);
});

test("保存期间编辑不会改变导出文件与本机恢复快照的一致性", async () => {
  const {context, state, storage} = harness();
  const response = deferred(), entered = deferred();
  let exported;
  context.exportArtifact = async payload => {
    exported = JSON.parse(JSON.stringify(payload.workspace));
    entered.resolve();
    await response.promise;
  };
  const saving = context.saveWorkspace();
  await entered.promise;
  state.strategy.nodes.find(node => node.op === "ma").window = 6;
  state.positions.price.x = 120;
  state.revision++;
  response.resolve();
  await saving;
  const restored = JSON.parse(storage.get("finblocks-workspace-v1"));
  assert.deepEqual(restored, exported);
  assert.equal(restored.strategy.nodes.find(node => node.op === "ma").window, 5);
  assert.equal(restored.view.positions.price.x, 20);
});

test("保存期间切换账号不会把原账号快照写到新账号", async () => {
  const {context, state, storage} = harness();
  state.bootstrap.user = {id: "scope_a", username: "test_a"};
  const response = deferred(), entered = deferred();
  context.exportArtifact = async () => {entered.resolve();await response.promise;};
  const saving = context.saveWorkspace();await entered.promise;
  state.bootstrap.user = {id: "scope_b", username: "test_b"};
  response.resolve();await saving;
  assert.equal(storage.has("finblocks-workspace-v1-user-scope_a"), true);
  assert.equal(storage.has("finblocks-workspace-v1-user-scope_b"), false);
});

test("账号切换后到达的AI候选不进入当前账号", async () => {
  const {context, state} = harness();
  const response = deferred(), entered = deferred();
  state.bootstrap.ai_configured = true;
  context.api = async () => {entered.resolve();return response.promise;};
  const generation = context.generate();await entered.promise;
  vm.runInContext("identityRevision++", context);
  response.resolve({status:"ok",strategy:state.strategy,evidence:{model_returned:"TEST_DOUBLE",usage:{total_tokens:0}}});
  await generation;
  assert.equal(state.candidate, null);
  assert.equal(context.document.getElementById("ai-candidate").hidden, true);
});

test("运行期间编辑会丢弃旧配置响应，保持结果为空", async () => {
  const {context, state, notices} = harness();
  const response = deferred(), entered = deferred();
  let applied = 0;
  context.renderResults = () => { applied++; };
  context.api = async (route, payload) => {
    assert.equal(route, "/api/backtest");
    assert.equal(payload.strategy.nodes.find(node => node.op === "ma").window, 5);
    entered.resolve();
    return response.promise;
  };
  const running = context.run();
  await entered.promise;
  state.strategy.nodes.find(node => node.op === "ma").window = 6;
  state.revision++;
  response.resolve({run_id: "test-only", report: {test_fixture_only: true}});
  await running;
  assert.equal(state.result, null);
  assert.equal(state.runId, null);
  assert.equal(state.runBusy, false);
  assert.equal(applied, 0);
  assert.ok(notices.some(message => message.includes("配置已改变")));
});

test("导入校验期间的新编辑不会被旧导入覆盖", async () => {
  const {context, state} = harness();
  const response = deferred(), entered = deferred();
  const candidate = JSON.parse(JSON.stringify(state.strategy));
  candidate.nodes.find(node => node.op === "ma").window = 10;
  context.api = async route => {
    assert.equal(route, "/api/validate");
    entered.resolve();
    return response.promise;
  };
  // 仅隔离图形重绘；状态应用仍由实际 applyWorkspace 完成。
  context.remember = () => {};
  context.defaultPositions = () => {};
  context.changed = () => { state.revision++; };
  const importing = context.applyWorkspace(candidate);
  await entered.promise;
  state.strategy.nodes.find(node => node.op === "ma").window = 6;
  state.revision++;
  response.resolve({valid: true});
  await assert.rejects(importing, /变化|改变|过时/);
  assert.equal(state.strategy.nodes.find(node => node.op === "ma").window, 6);
});

test("两个导入反序返回时仅应用最后发起的导入", async () => {
  const {context, state} = harness();
  const firstResponse = deferred(), secondResponse = deferred();
  const first = JSON.parse(JSON.stringify(state.strategy));
  const second = JSON.parse(JSON.stringify(state.strategy));
  first.nodes.find(node => node.op === "ma").window = 10;
  second.nodes.find(node => node.op === "ma").window = 20;
  context.api = async (route, payload) => {
    assert.equal(route, "/api/validate");
    return payload.strategy.nodes.find(node => node.op === "ma").window === 10 ? firstResponse.promise : secondResponse.promise;
  };
  context.remember = () => {};
  context.defaultPositions = () => {};
  context.changed = () => { state.revision++; };
  const firstImport = context.applyWorkspace(first);
  const secondImport = context.applyWorkspace(second);
  secondResponse.resolve({valid: true});
  await secondImport;
  assert.equal(state.strategy.nodes.find(node => node.op === "ma").window, 20);
  firstResponse.resolve({valid: true});
  await assert.rejects(firstImport, /变化|改变|过时/);
  assert.equal(state.strategy.nodes.find(node => node.op === "ma").window, 20);
});

test("无风险百分比转换为小数，保存包含实际年化配置", () => {
  const {context} = harness();
  context.document.getElementById("risk-free").value = "2";
  context.document.getElementById("periods").value = "244";
  assert.equal(context.workspaceFile().config.annual_risk_free_rate, 0.02);
  assert.equal(context.workspaceFile().config.periods_per_year, 244);
});

test("旧工作台导入补齐统计默认值，无效新利率不会改变策略", async () => {
  const {context, state} = harness();
  context.api = async () => ({valid: true});
  context.remember = () => {};
  context.defaultPositions = () => {};
  context.changed = () => { state.revision++; };
  const legacy = {format: "finblocks-workspace", version: 1, strategy: JSON.parse(JSON.stringify(state.strategy)),
    config: {symbol: "sh600455", start: "2024-01-01", end: "2026-08-19", cost_bps: 10, lag: 1}};
  await context.applyWorkspace(legacy);
  assert.equal(Number(context.document.getElementById("periods").value), 252);
  assert.equal(Number(context.document.getElementById("risk-free").value), 0);
  const invalid = JSON.parse(JSON.stringify(legacy));
  invalid.strategy.nodes[1].window = 7;
  invalid.config.annual_risk_free_rate = -1;
  await assert.rejects(context.applyWorkspace(invalid), /年化周期或无风险利率无效/);
  assert.equal(state.strategy.nodes[1].window, legacy.strategy.nodes[1].window);
});

test("技术指标与常量是数值输入，候选描述展示真实分量", () => {
  const {context} = harness();
  const strategy = JSON.parse(fs.readFileSync(path.join(root, "examples/macd_strategy.json"), "utf8"));
  for (const op of ["const", "ema", "rsi", "bollinger", "macd"]) assert.equal(context.numeric({op}), true);
  const description = context.describeStrategy(strategy);
  assert.ok(description.includes("DIF") && description.includes("DEA"));
  assert.equal(context.numeric({op: "gt"}), false);
});

test("真实四股统计绘图保留负值及时间序列与IC的不同名称", {skip: !fs.existsSync(path.join(root,"artifacts/m8_factor_cross_section.json")) && "共享包不含本地真实因子证据"}, () => {
  const {context}=harness();vm.runInContext(fs.readFileSync(path.join(root,"web/research.js"),"utf8"),context);
  const result=JSON.parse(fs.readFileSync(path.join(root,"artifacts/m8_factor_cross_section.json"),"utf8"));
  const html=context.factorVisualization(result);
  assert.ok(html.includes(`IC：${result.validation.correlation.toFixed(6)}`));
  assert.ok(html.includes('class="negative"'));
  result.protocol.mode="time_series";
  const timeSeries=context.factorVisualization(result);
  assert.ok(timeSeries.includes("时间序列秩相关"));
  assert.ok(!timeSeries.includes("Rank IC"));
});

test("冻结前图表不读取测试指标或将不可用相关填零", () => {
  const {context}=harness();vm.runInContext(fs.readFileSync(path.join(root,"web/research.js"),"utf8"),context);
  const result={protocol:{mode:"time_series"},train:{pairs:30,dates:30,correlation:null,rank_correlation:null},validation:{pairs:30,dates:30,correlation:0,rank_correlation:-.5},test_revealed:false};
  Object.defineProperty(result,"test",{get(){throw new Error("禁止提前读取测试指标");}});
  const html=context.factorVisualization(result);
  assert.ok(html.includes("不可用"));assert.ok(html.includes("尚未查看"));
  assert.equal((html.match(/role="img"/g)||[]).length,2);
});

test("图中风险状态保留警告语义并转义用户文本", () => {
  const {context}=harness();vm.runInContext(fs.readFileSync(path.join(root,"web/research.js"),"utf8"),context);
  const html=context.researchTable(["状态"],[["证据不足"],["未发现明显异常"],["<script>测试</script>"]]);
  assert.ok(html.includes('class="risk-pill warning"'));
  assert.ok(html.includes('class="risk-pill reviewed"'));assert.ok(html.includes("&lt;script&gt;"));assert.ok(!html.includes("<script>"));
});

test("研究失效同时清除键盘图表读取的旧账号数据", () => {
  const {context,state}=harness();context.renderLedger=()=>{};context.drawChart=()=>{};
  context.document.getElementById("chart-readout").textContent="原账号净值";state.chartFocusIndex=5;
  context.invalidate();assert.equal(state.chartFocusIndex,null);assert.equal(state.chartGeometry,null);
  assert.ok(!context.document.getElementById("chart-readout").textContent.includes("原账号"));
});

test("真实净值的画布坐标、悬停与单条记录均为有限值", {skip: !fs.existsSync(path.join(root,"artifacts/m8_converted_backtest.json")) && "共享包不含本地真实回测证据"}, () => {
  const {context,state}=harness();const coordinates=[];
  const ctx={scale(){},fillRect(){},fillText(){},beginPath(){},stroke(){},fill(){},closePath(){},setLineDash(){},createLinearGradient(){return {addColorStop(){}};},moveTo(...v){coordinates.push(...v);},lineTo(...v){coordinates.push(...v);},arc(x,y){coordinates.push(x,y);}};
  const canvas=context.document.getElementById("chart");canvas.getBoundingClientRect=()=>({width:320,height:260});canvas.getContext=()=>ctx;context.window={devicePixelRatio:2};
  state.result=JSON.parse(fs.readFileSync(path.join(root,"artifacts/m8_converted_backtest.json"),"utf8"));
  const original=JSON.stringify(state.result.records);context.drawChart(20);
  const first=state.result.records[0],last=state.result.records.at(-1),g=state.chartGeometry;
  assert.equal(g.x(0),g.pad.left);assert.equal(g.x(state.result.records.length-1),g.w-g.pad.right);
  assert.ok(coordinates.every(Number.isFinite));assert.equal(JSON.stringify(state.result.records),original);
  state.result.records=[first];coordinates.length=0;context.drawChart(0);assert.ok(coordinates.every(Number.isFinite));
  assert.ok(Number.isFinite(state.chartGeometry.y(last.equity)));
});

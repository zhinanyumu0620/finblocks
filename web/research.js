"use strict";

// 所有异步结果绑定账号、画布和研究表单版本，防止旧响应串入新研究。
const researchState={version:0,evidence:null,date:null,repair:null,candidate:null,validation:null,test:null,conversion:null,busy:new Set()};
function resetResearch(){
  researchState.version++;researchState.evidence=null;researchState.date=null;researchState.repair=null;researchState.validation=null;researchState.test=null;researchState.conversion=null;
  for(const id of ["audit-result","evidence-result","explain-result","experiment-result","factor-result","factor-explanation","factor-conversion"])if($(id))$(id).textContent="配置已改变，请按当前配置重新研究。";
  for(const id of ["repair-apply-btn","factor-convert-apply"])if($(id))$(id).hidden=true;
  for(const id of ["factor-test-btn","factor-explain-btn"])if($(id))$(id).disabled=true;
  if($("research-history"))$("research-history").textContent="点击刷新查看当前身份的记录。";
  if(researchState.identity!==identityRevision){
    researchState.candidate=null;researchState.identity=identityRevision;
    for(const id of ["research-intent","intent-field","intent-relation","intent-fast","intent-slow","intent-true","intent-false","factor-question","factor-expression","factor-symbols","factor-start","factor-end","factor-threshold","factor-true","factor-false","experiment-values"])if($(id))$(id).value="";
    for(const id of ["explain-consent","experiment-confirm","factor-ai-consent","factor-protocol-confirm","factor-test-confirm","factor-convert-confirm"])if($(id))$(id).checked=false;
    for(const id of ["factor-candidate","factor-description","factor-conversion","research-message","factor-message"])if($(id))$(id).textContent="";
    if($("factor-apply-candidate"))$("factor-apply-candidate").hidden=true;
  }
}
async function researchAction(id, work){
  if(researchState.busy.has(id))return;
  researchState.busy.add(id);const button=$(id),identity=identityRevision,revision=state.revision,version=researchState.version;button.disabled=true;
  const messageId=id.startsWith("factor-")?"factor-message":"research-message";$(messageId).textContent="";
  const current=()=>identity===identityRevision&&revision===state.revision&&version===researchState.version;
  try{await work(current);}catch(error){if(current()){notice(error.message,true);$(messageId).textContent=error.message;}}
  finally{researchState.busy.delete(id);button.disabled=["factor-generate-btn","explain-btn"].includes(id)?!state.bootstrap.ai_configured:id==="factor-test-btn"?!researchState.validation:id==="factor-explain-btn"?!researchState.validation||!state.bootstrap.ai_configured:false;}
}
function researchTable(headers,rows){const statuses={"证据不足":"warning","未执行":"warning","发现风险信号":"danger","未发现明显异常":"reviewed","已执行":"reviewed","failed":"danger","completed":"reviewed"};return `<div class="table-wrap"><table><thead><tr>${headers.map(h=>`<th scope="col">${escapeHtml(h)}</th>`).join("")}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map(value=>`<td>${Object.hasOwn(statuses,value)?`<span class="risk-pill ${statuses[value]}">${escapeHtml(value)}</span>`:escapeHtml(value??"不可用")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;}
function intentContract(){
  const result={};for(const [id,key] of [["intent-field","field"],["intent-relation","relation"]])if($(id).value)result[key]=$(id).value;
  if($("intent-fast").value||$("intent-slow").value){if(!$("intent-fast").value||!$("intent-slow").value)throw new Error("MA契约须明确填写左右两个窗口");result.ma_windows=[Number($("intent-fast").value),Number($("intent-slow").value)];}
  for(const [id,key] of [["intent-true","when_true"],["intent-false","when_false"]])if($(id).value!=="")result[key]=Number($(id).value)/100;
  return Object.keys(result).length?result:null;
}
function renderIntentAudit(result){
  $("audit-result").innerHTML=`<b>${escapeHtml(result.status)}</b><p>${escapeHtml(result.scope)}</p>${researchTable(["意图项","期望","实际","状态","积木"],result.checks.map(c=>[c.slot,JSON.stringify(c.expected),JSON.stringify(c.actual),c.status,c.node_ids.join(", ")]))}<p class="negative">${escapeHtml(result.blockers.join("；"))}</p>`;
}
function renderEvidence(pack){
  researchState.evidence=pack;researchState.date=pack.day;
  $("evidence-result").innerHTML=`<b>${pack.day?escapeHtml(pack.day)+" 的执行证据":"本次回测证据"}</b>${researchTable(["证据编号","事实","实际值"],pack.facts.map(f=>[f.id,f.label,typeof f.value==="object"?JSON.stringify(f.value):f.value]))}<p>${escapeHtml(pack.limitations.join("；"))}</p>`;
  const nodeFacts=pack.facts.filter(f=>f.node);if(nodeFacts.length)$("evidence-result").insertAdjacentHTML("beforeend",`<div class="research-actions">${nodeFacts.map(f=>`<button class="button compact secondary" data-node-evidence="${escapeHtml(f.node.id)}">定位 ${escapeHtml(f.node.id)}</button>`).join("")}</div>`);
  $("explain-result").textContent="尚未调用AI解释。原始事实由本地程序生成。";
}
async function showEvidence(day=null){return researchAction("evidence-overview",async current=>{if(!state.runId)throw new Error("请先按当前配置运行回测");const result=await api("/api/research/evidence",{run_id:state.runId,date:day});if(current()){renderEvidence(result);$("evidence-result").scrollIntoView({block:"center"});}});}
function renderClaims(id,result){$(id).innerHTML=`<b>实际AI解释 · ${escapeHtml(result.evidence.model_returned)}</b><p>${escapeHtml(result.validation_scope)}</p>${result.claims.map(c=>`<p><code>${escapeHtml(c.evidence_id)}</code> ${escapeHtml(c.explanation)}</p>`).join("")}<p>实际用量：${escapeHtml(result.evidence.usage?.total_tokens??"未返回")} tokens</p>`;}
function renderFactorDescription(result){
  $("factor-description").innerHTML=`<b>结构类型与文献参考</b>${researchTable(["类型","状态","依据节点"],result.categories.map(c=>[c.type,c.status,c.evidence.join(", ")]))}<p>${escapeHtml(result.interpretation)}</p><p>关键词：${escapeHtml(result.keywords.join(" · "))}</p><p>${escapeHtml(result.literature_mode)}</p><div class="reference-grid">${result.references.map(r=>`<article class="reference-card"><a href="${escapeHtml(r.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(r.title)}</a><p>${escapeHtml(r.authors)} · ${r.year} · ${escapeHtml(r.identifier)}</p><p>${escapeHtml(r.relation)}</p><small>${escapeHtml(r.read_scope)}；核验日期 ${escapeHtml(r.verified_on)}</small></article>`).join("")}</div>`;
  if(result.structural_findings?.length)$("factor-description").insertAdjacentHTML("afterbegin",researchTable(["结构语义检查","确定性发现"],result.structural_findings.map(f=>[f.node_id,f.finding])));
  $("factor-description").insertAdjacentHTML("beforeend",`<label>检索关键词（可选中复制）<textarea readonly rows="2">${escapeHtml(result.keywords.join("; "))}</textarea></label>`);
}
function factorRequest(){
  if(!$("factor-protocol-confirm").checked)throw new Error("请先确认公式、股票集合、日期与分区协议");
  if(!$("factor-start").value||!$("factor-end").value)throw new Error("因子实验须明确填写日期，不自动缩短样本");
  return {expression:$("factor-expression").value,symbols:$("factor-symbols").value.split(/[,，\s]+/).filter(Boolean),start:$("factor-start").value,end:$("factor-end").value,
    ...($("data-source")?.value==="public_hfq"?{data_source:"public_hfq"}:{}),protocol:{mode:$("factor-mode").value,horizon:Number($("factor-horizon").value),train_fraction:Number($("factor-train").value)/100,validation_fraction:Number($("factor-validation").value)/100,min_samples:Number($("factor-min-samples").value)},confirmed:true};
}
// 固定[-1,1]坐标只展示实际相关统计；冻结前不读取测试指标。
function factorVisualization(result){
  const isCross=result.protocol.mode==="cross_section";
  const names=isCross?["IC","Rank IC"]:["时间序列相关","时间序列秩相关"];
  const phases=[["train","训练区间"],["validation","验证区间"],["test","测试区间"]];
  const bar=(label,value)=>{
    if(!Number.isFinite(value)||Math.abs(value)>1)return `<div class="factor-bar-label"><span>${label}</span><strong>不可用</strong></div>`;
    const width=Math.abs(value)*50,left=value<0?50-width:50;
    return `<div class="factor-bar-label"><span>${label}</span><strong>${value.toFixed(4)}</strong></div><div class="factor-bar" role="img" aria-label="${label}：${value.toFixed(6)}，范围负一至一"><span class="${value<0?"negative":"positive"}" style="left:${left}%;width:${width}%"></span></div><div class="factor-scale" aria-hidden="true"><span>−1</span><span>0</span><span>+1</span></div>`;
  };
  return `<section class="factor-visual" aria-label="因子分区评价图"><h4>分区评价概览</h4><p>${isCross?"逐日横截面相关的平均值":"单股时间序列相关"} · 正负方向均保留；相关不代表交易收益或因果。</p><div class="factor-phase-grid">${phases.map(([key,title])=>{
    if(key==="test"&&!result.test_revealed)return `<article class="factor-phase pending"><h5>${title}</h5><p>尚未查看。冻结候选后单独评价，不展示测试区间得分。</p></article>`;
    const values=result[key];return `<article class="factor-phase"><h5>${title}</h5><p>${escapeHtml(values.pairs??"不可用")} 个有效配对 · ${escapeHtml(values.valid_cross_sections??values.dates??"不可用")} ${isCross?"有效截面":"日期"}</p>${bar(names[0],values.correlation)}${bar(names[1],values.rank_correlation)}</article>`;
  }).join("")}</div></section>`;
}
function renderFactorResult(result){
  if(result.status!=="ok"){$("factor-result").innerHTML=`<b>实验被阻断</b><p>${escapeHtml(result.reason)}</p>${researchTable(["标的","实际拒绝原因"],result.rejected.map(r=>[r.symbol,r.reason]))}`;return;}
  renderFactorDescription(result.description);
  const rows=[...["train","validation"].map(k=>[k,result[k].pairs,result[k].dates,result[k].correlation,result[k].rank_correlation,result[k].group_spread]),result.test_revealed?["test",result.test.pairs,result.test.dates,result.test.correlation,result.test.rank_correlation,result.test.group_spread]:["test","尚未执行",result.test.dates,"冻结后评估","冻结后评估","冻结后评估"]];
  $("factor-result").innerHTML=`<b>${result.test_revealed?"最终测试已执行":"训练/验证已完成，最终测试未查看"}</b><p>${escapeHtml(result.statistics_scope)}</p>${factorVisualization(result)}${researchTable(["区间","有效配对数","日期数","相关 / IC","秩相关 / Rank IC","分组差（非交易收益）"],rows)}<p>区间：${escapeHtml(JSON.stringify(result.intervals))}</p><p>${escapeHtml(result.stock_pool_limitations)}；${escapeHtml(result.test_independence)}</p><h3>风险审查</h3>${researchTable(["检查","状态","证据/限制"],result.risks.map(r=>[r.item,r.status,typeof r.evidence==="string"?r.evidence:JSON.stringify(r.evidence)]))}<p class="fine-print">${escapeHtml(result.overfitting_conclusion)}。PBO/DSR未实现，文献仅供方法参考。</p>`;
  if(result.test.unavailable_reason)$("factor-result").insertAdjacentHTML("beforeend",`<p class="negative">测试不可用：${escapeHtml(result.test.unavailable_reason)}</p>`);
  if(result.duplicate_check)$("factor-result").insertAdjacentHTML("beforeend",`<p>结构重复检查：${result.duplicate_check.matching_recent_research_ids.length} 条近期相同结构。${escapeHtml(result.duplicate_check.scope)}</p>`);
}
function installResearch(){
  researchState.identity=identityRevision;
  $("research-panel").insertAdjacentHTML("afterbegin",'<p id="research-message" class="negative" role="status"></p>');
  Object.assign(TERMS,{std:{title:"滚动标准差 STD",text:"本项目按窗口内实际输入计算总体标准差ddof=0，预热不足时不可用。"},lag:{title:"历史滞后 LAG",text:"仅使用指定条数之前的真实记录，不允许向未来取数。"},factor:{title:"因子",text:"由当时可得信息计算的观察指标；因子相关不等于可交易收益，也不证明因果。"},overfit:{title:"过拟合",text:"对有限历史样本及选择过程适配过强，可能无法推广。样本外、参数敏感性和多重尝试提供诊断证据，未检出问题不代表安全。"},ic:{title:"IC / Rank IC",text:"横截面因子值与同协议未来收益的Pearson/Spearman相关。单股时间序列相关单独命名；样本或方差不足时无定义。"},arithmetic:{title:"算术因子积木",text:"加减乘除只操作当时可得数值。除零和非有限结果保留不可用，不能填零来获得回测成绩。"}});
  $("factor-panel").querySelector("h2").insertAdjacentHTML("beforeend",termHelp("factor")+termHelp("ic")+termHelp("overfit"));
  $("question-template").addEventListener("click",()=>{$("research-intent").value={trend:"我想研究：以哪一个行情字段计算两条MA，持续高于与上穿分别怎样影响持仓？请先确认窗口、条件与持仓比例。",volume:"我想研究：价格相对均线偏离与成交量相对均线的活跃度，是否关联之后的走势？请先确认两个窗口和预测跨度。",volatility:"我想研究：滚动波动率是否关联后续收益？请先确认字段、STD窗口、预测跨度与检验区间。"}[$("research-question").value];notice("已填写研究问题，尚未补参数或调用AI。");});
  $("audit-btn").addEventListener("click",()=>researchAction("audit-btn",async current=>{const contract=intentContract(),result=await api("/api/research/audit",{prompt:$("research-intent").value,strategy:state.strategy,...(contract?{contract}:{})});if(current())renderIntentAudit(result);}));
  $("repair-preview-btn").addEventListener("click",()=>researchAction("repair-preview-btn",async current=>{const contract=intentContract();if(!contract)throw new Error("请在契约中明确要修复的字段");const result=await api("/api/research/repair",{prompt:$("research-intent").value,strategy:state.strategy,contract});if(current()){researchState.repair=result;renderIntentAudit(result.audit);$("audit-result").insertAdjacentHTML("beforeend",`<pre>${escapeHtml(JSON.stringify({nodes:result.diff,allocation:result.allocation_diff},null,2))}</pre>`);$("repair-apply-btn").hidden=result.audit.application_blocked;}}));
  $("repair-apply-btn").addEventListener("click",()=>{const result=researchState.repair;if(!result||result.audit.application_blocked)return;remember();state.strategy=clone(result.strategy);state.generationId=null;defaultPositions();changed();notice("已明确应用双MA修复，执行配置未更改，请重新校验与回测。");});
  $("evidence-overview").addEventListener("click",()=>showEvidence());
  $("ledger").addEventListener("click",event=>{const target=event.target.closest("[data-evidence-date]");if(target)showEvidence(target.dataset.evidenceDate);});
  $("evidence-result").addEventListener("click",event=>{const target=event.target.closest("[data-node-evidence]");if(!target)return;const node=[...document.querySelectorAll(".node")].find(n=>n.dataset.id===target.dataset.nodeEvidence);if(node){node.scrollIntoView({block:"center"});node.classList.add("guide-highlight");setTimeout(()=>node.classList.remove("guide-highlight"),1800);}});
  $("explain-btn").addEventListener("click",()=>researchAction("explain-btn",async current=>{if(!researchState.evidence||!$("explain-consent").checked)throw new Error("先选择真实证据，并确认发送摘要");const result=await api("/api/research/explain",{run_id:state.runId,date:researchState.date,confirmed:true});if(current())renderClaims("explain-result",result);}));
  $("experiment-btn").addEventListener("click",()=>researchAction("experiment-btn",async current=>{if(!state.runId||!$("experiment-confirm").checked)throw new Error("先运行回测，并确认单因素协议");const raw=$("experiment-values").value.trim();if(!raw)throw new Error("请明确填写实验值");const result=await api("/api/research/experiment",{run_id:state.runId,axis:$("experiment-axis").value,values:raw.split(/[,，\s]+/).map(Number),confirmed:true});if(current())$("experiment-result").innerHTML=researchTable(["因素值","状态","累计收益","最大回撤","夏普 / 失败原因"],result.results.map(r=>[r.value,r.status,r.metrics?.total_return,r.metrics?.max_drawdown,r.metrics?.sharpe_ratio??r.reason]))+`<p>${escapeHtml(result.scope)}</p>`;}));
  $("factor-generate-btn").disabled=!state.bootstrap.ai_configured;
  $("explain-btn").disabled=!state.bootstrap.ai_configured;
  $("factor-generate-btn").addEventListener("click",()=>researchAction("factor-generate-btn",async current=>{if(!$("factor-ai-consent").checked)throw new Error("请确认调用一个真实AI候选");const result=await api("/api/factor/generate",{prompt:$("factor-question").value,confirmed:true});if(current()){researchState.candidate=result;$("factor-candidate").textContent=result.status==="ok"?`${result.expression}\n假设（待验证）：${result.hypothesis}\n失效条件：${result.failure_conditions}\n模型：${result.evidence.model_returned}`:result.reason;$("factor-apply-candidate").hidden=result.status!=="ok";if(result.description)renderFactorDescription(result.description);}}));
  $("factor-apply-candidate").addEventListener("click",()=>{if(researchState.candidate?.status!=="ok")return;$("factor-expression").value=researchState.candidate.expression;factorChanged();notice("已确认使用候选公式，尚未执行实验。");});
  $("factor-inspect-btn").addEventListener("click",()=>researchAction("factor-inspect-btn",async current=>{const result=await api("/api/factor/inspect",{expression:$("factor-expression").value});if(current())renderFactorDescription(result);}));
  const factorChanged=()=>{researchState.version++;researchState.validation=null;researchState.test=null;researchState.conversion=null;$("factor-result").textContent="因子或协议已改变，请重新评价；已有记录保留。";$("factor-test-btn").disabled=true;$("factor-explain-btn").disabled=true;$("factor-test-confirm").checked=false;$("factor-convert-apply").hidden=true;$("factor-explanation").textContent="";};
  for(const id of ["factor-expression","factor-mode","factor-symbols","factor-start","factor-end","factor-horizon","factor-min-samples","factor-train","factor-validation"])$(id).addEventListener("input",factorChanged);
  for(const id of ["research-intent","intent-field","intent-relation","intent-fast","intent-slow","intent-true","intent-false"])$(id).addEventListener("input",()=>{researchState.version++;researchState.repair=null;$("repair-apply-btn").hidden=true;$("audit-result").textContent="意图已改变，请重新审计。";});
  for(const id of ["experiment-axis","experiment-values"])$(id).addEventListener("input",()=>{researchState.version++;$("experiment-result").textContent="实验协议已改变，请重新确认并运行。";$("experiment-confirm").checked=false;});
  $("factor-use-current").addEventListener("click",()=>{$("factor-symbols").value=$("symbol").value;$("factor-start").value=$("start").value;$("factor-end").value=$("end").value;factorChanged();});
  $("factor-use-pool").addEventListener("click",()=>{$("factor-symbols").value=state.bootstrap.symbols.filter(s=>s.pool==="csi300").map(s=>s.code).join(",");$("factor-mode").value="cross_section";factorChanged();notice("已明确选择当前沪深300名单，未运行。请确认日期、质量与分区条件。");});
  $("factor-evaluate-btn").addEventListener("click",()=>researchAction("factor-evaluate-btn",async current=>{const result=await api("/api/factor/evaluate",factorRequest());if(current()){renderFactorResult(result);researchState.validation=result.status==="ok"?result:null;$("factor-test-btn").disabled=result.status!=="ok";$("factor-explain-btn").disabled=result.status!=="ok"||!state.bootstrap.ai_configured;}}));
  $("factor-explain-btn").addEventListener("click",()=>researchAction("factor-explain-btn",async current=>{if(!researchState.validation||!$("explain-consent").checked)throw new Error("请在证据区确认向AI发送摘要；该解读只发送训练/验证，不发送最终测试");const result=await api("/api/factor/explain",{research_id:researchState.validation.research_id,confirmed:true});if(current())renderClaims("factor-explanation",result);}));
  $("factor-test-btn").addEventListener("click",()=>researchAction("factor-test-btn",async current=>{if(!researchState.validation||!$("factor-test-confirm").checked)throw new Error("请确认冻结当前候选并查看最终测试");const result=await api("/api/factor/test",{research_id:researchState.validation.research_id,confirmed:true});if(current()){researchState.test=result;renderFactorResult(result);}}));
  $("factor-convert-btn").addEventListener("click",()=>researchAction("factor-convert-btn",async current=>{if(!$("factor-convert-confirm").checked||["factor-threshold","factor-true","factor-false"].some(id=>$(id).value===""))throw new Error("请明确填写并确认阈值和持仓比例");const result=await api("/api/factor/convert",{expression:$("factor-expression").value,threshold:Number($("factor-threshold").value),direction:$("factor-direction").value,allocation:{when_true:Number($("factor-true").value)/100,when_false:Number($("factor-false").value)/100},confirmed:true,...(researchState.candidate?.status==="ok"?{research_id:researchState.candidate.research_id}:{})});if(current()){researchState.conversion=result;$("factor-conversion").textContent=describeStrategy(result.strategy);$("factor-convert-apply").hidden=false;}}));
  for(const id of ["factor-threshold","factor-direction","factor-true","factor-false"])$(id).addEventListener("input",()=>{researchState.version++;researchState.conversion=null;$("factor-convert-apply").hidden=true;});
  $("factor-convert-apply").addEventListener("click",()=>{const result=researchState.conversion;if(!result)return;remember();state.strategy=clone(result.strategy);state.generationId=result.generation_id;defaultPositions();changed();notice("已确认应用因子阈值策略，请在上方按真实区间重新回测。不是因子已有效的证明。");});
  $("research-history-btn").addEventListener("click",()=>researchAction("research-history-btn",async current=>{const result=await api("/api/research/history",{});if(current())$("research-history").innerHTML=result.records.length?result.records.map(r=>`<details class="reference-card"><summary>${escapeHtml(r.kind)} · ${escapeHtml(r.created_at_utc)}</summary><button class="button compact secondary" data-research-export="${r.id}">导出该研究JSON</button><pre>${escapeHtml(JSON.stringify(r.document,null,2))}</pre></details>`).join(""):"当前身份尚无研究记录。";}));
  $("research-history").addEventListener("click",event=>{const target=event.target.closest("[data-research-export]");if(target)exportArtifact({kind:"research",research_id:target.dataset.researchExport}).catch(error=>notice(error.message,true));});
}

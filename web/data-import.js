"use strict";

// 上传绑定发起时的身份，迟到响应不得切换另一个账号的数据源。
let csvImportBusy=false;
function renderDataSourceOptions(){
  const selected=$("data-source").value||state.bootstrap.default_data_source||"original",sources=state.bootstrap.data_sources||{};
  $("data-source").innerHTML='<option value="original">原始数据 · 严格质量检查</option>'+Object.entries(sources).map(([id,info])=>`<option value="${escapeHtml(id)}">${escapeHtml(info.label)}</option>`).join("");
  $("data-source").value=selected==="original"||sources[selected]?selected:"sample" in sources?"sample":"original";
}
async function refreshDataSources(){
  const identity=identityRevision,bootstrap=await api("/api/bootstrap");
  if(identity!==identityRevision)return false;
  state.bootstrap=bootstrap;renderDataSourceOptions();return true;
}
function selectDataCollection(){
  const custom=isCSVSource($("data-source").value);
  $("stock-pool").value=custom?"custom":$("stock-pool").value==="custom"?"csi300":$("stock-pool").value;
  $("symbol-search").value="";renderSymbolOptions("",true);
  const first=selectedSymbol();$("start").value=first?.start||"";$("end").value=first?.end||"";$("portfolio-symbols").value="";
  $("source-count").textContent=custom?`${state.bootstrap.data_sources[$("data-source").value].label} · ${availableSymbols().length} 个标的`:`沪深300 · ${state.bootstrap.stock_pool.summary.official_members} 个成员 · ${state.bootstrap.stock_pool.as_of}`;
}
async function loadSampleCase(){
  if(!state.bootstrap.data_sources?.sample)throw new Error("内置案例文件未就绪，请核对源码包");
  const identity=identityRevision,document=await api("/api/data/sample-case.json");if(identity!==identityRevision)return;
  await applyWorkspace(document);if(identity!==identityRevision)return;
  $("factor-expression").value="close / MA(close, 20) - 1";$("factor-symbols").value=document.config.symbols.join(", ");$("factor-start").value=document.config.start;$("factor-end").value=document.config.end;
  $("factor-mode").value="cross_section";$("factor-horizon").value=5;$("factor-train").value=60;$("factor-validation").value=20;$("factor-min-samples").value=5;$("factor-protocol-confirm").checked=false;
  notice("已载入真实历史案例。先检查股票池数据，再运行回测；因子另需确认协议，不会自动调用AI。");
}
async function importCSV(event){
  event.preventDefault();if(csvImportBusy)return;
  const file=$("csv-file").files?.[0];if(!file)throw new Error("请选择CSV文件");
  if(file.size>2000000){$("csv-message").textContent="CSV须不超过2MB（项目预算）。";return;}
  const identity=identityRevision;csvImportBusy=true;$("csv-submit").disabled=true;$("csv-message").textContent="正在校验整份CSV…";
  // 在等待文件读取前冻结来源声明，避免用户编辑导致记录与实际提交不一致。
  const payload={label:$("csv-label").value,source_notice:$("csv-source").value,price_basis:$("csv-basis").value,volume_unit:"shares",currency:$("csv-currency").value,confirmed:$("csv-confirm").checked};
  try{
    const bytes=new Uint8Array(await file.arrayBuffer());let binary="";for(const byte of bytes)binary+=String.fromCharCode(byte);
    if(identity!==identityRevision)return;
    payload.csv_base64=btoa(binary);
    const result=await api("/api/data/import-csv",payload);
    if(identity!==identityRevision)return;
    state.bootstrap.data_sources={...state.bootstrap.data_sources,[result.source_id]:result.source};renderDataSourceOptions();$("data-source").value=result.source_id;selectDataCollection();
    invalidate();await validateCurrent();if(identity!==identityRevision)return;$("csv-message").textContent=`导入成功：${result.source.summary.ready}个标的，${result.source.summary.rows}条记录。来源与口径将随报告保存。`;
    $("csv-file").value="";$("csv-dialog").close();notice($("csv-message").textContent);
  }catch(error){if(identity===identityRevision)$("csv-message").textContent="导入失败："+error.message;}
  finally{csvImportBusy=false;$("csv-submit").disabled=false;}
}
function resetCSVImport(){
  $("csv-dialog").close();$("csv-form").reset();$("csv-message").textContent="";
}
function installDataImport(){
  $("data-import-btn").addEventListener("click",()=>{$("csv-message").textContent="";$("csv-file").value="";$("csv-confirm").checked=false;$("csv-dialog").showModal();});
  $("csv-close").addEventListener("click",()=>$("csv-dialog").close());
  $("csv-form").addEventListener("submit",event=>{importCSV(event).catch(error=>{$("csv-message").textContent=error.message;});});
  $("sample-case-btn").addEventListener("click",()=>loadSampleCase().catch(error=>notice(error.message,true)));
}

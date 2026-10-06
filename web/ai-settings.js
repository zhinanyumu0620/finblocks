"use strict";

// 页面只保存不含密钥的连接状态；密钥输入不会进入localStorage或工作台导出。
let aiSettingsBusy=false;
function applyAIProfile(profile){
  state.bootstrap.ai_profile=profile;state.bootstrap.ai_configured=profile.configured;
  const provider=state.bootstrap.ai_providers?.find(p=>p.id===profile.provider)?.label||profile.provider;
  $("ai-status").textContent=profile.configured?`${profile.source==="personal"?"个人AI":"工作台默认AI"} · ${provider} / ${profile.model}`:"AI 未配置 · 可人工构建";
  $("generate-btn").disabled=state.aiBusy||!profile.configured;
  for(const id of ["factor-generate-btn","explain-btn"])if($(id))$(id).disabled=!profile.configured;
  if($("factor-explain-btn"))$("factor-explain-btn").disabled=!profile.configured||typeof researchState==="undefined"||!researchState.validation;
}
function clearAIInput(){ $("ai-api-key").value="";$("ai-api-key").type="password";$("ai-show-key").checked=false; }
async function refreshAISettings(){
  clearAIInput();if($("ai-settings-dialog").open)$("ai-settings-dialog").close();
  const identity=identityRevision;
  if(!state.bootstrap.user){const bootstrap=await api("/api/bootstrap");if(identity===identityRevision)applyAIProfile(bootstrap.ai_profile);return;}
  const result=await api("/api/ai/config");if(identity!==identityRevision)return;
  state.bootstrap.ai_providers=result.providers;state.bootstrap.ai_remember_supported=result.remember_supported;
  applyAIProfile(result.profile);
}
function chooseAIPreset(id){
  const preset=state.bootstrap.ai_providers.find(p=>p.id===id);if(!preset)return;
  clearAIInput();$("ai-provider").value=id;$("ai-base-url").value=preset.base_url;$("ai-model").value=preset.model;
  $("ai-remember").checked=false;$("ai-settings-result").textContent="尚未测试连接。模型示例可编辑，需拥有对应API权限。";
  $("ai-provider-docs").hidden=!preset.docs_url;$("ai-provider-docs").href=preset.docs_url||"#";
}
function openAISettings(preferred){
  if(!state.bootstrap.user){openAccount();$("auth-ai-provider").value=preferred||"deepseek";return;}
  if($("welcome-dialog").open)$("welcome-dialog").close();stopTour();
  const profile=state.bootstrap.ai_profile;
  chooseAIPreset(preferred||profile.provider||"deepseek");
  if(!preferred&&profile.source==="personal"){$("ai-base-url").value=profile.base_url;$("ai-model").value=profile.model;$("ai-remember").checked=profile.remembered&&state.bootstrap.ai_remember_supported;}
  $("ai-api-key").placeholder=profile.source==="personal"&&profile.configured?"留空保留当前地址的密钥；更换服务商请重新填写":"粘贴该服务商的API Key";
  $("ai-remember").disabled=!state.bootstrap.ai_remember_supported;
  $("ai-storage-note").textContent=state.bootstrap.ai_remember_supported?"默认密钥仅用于本次登录。勾选后由Windows加密记住，绑定这台电脑的系统账户。":"当前系统仅支持本次登录使用密钥，退出或重启后需要重新填写。";
  $("ai-settings-dialog").showModal();$("ai-provider").focus();
}
function aiSettingsPayload(){return {provider:$("ai-provider").value,base_url:$("ai-base-url").value.trim(),model:$("ai-model").value.trim(),api_key:$("ai-api-key").value.trim(),remember:$("ai-remember").checked};}
async function submitAISettings(action){
  if(aiSettingsBusy)return;aiSettingsBusy=true;const identity=identityRevision;
  const payload=action==="delete"?{}:aiSettingsPayload();
  for(const id of ["ai-save","ai-test","ai-delete","ai-settings-close"])$(id).disabled=true;
  for(const id of ["ai-provider","ai-base-url","ai-model","ai-api-key","ai-show-key","ai-remember"])$(id).disabled=true;
  $("ai-settings-result").textContent=action==="test"?"正在向当前地址发送短JSON请求…":"正在更新个人配置…";
  try{
    const result=await api("/api/ai/"+(action==="save"?"config":action),payload);
    if(identity!==identityRevision)return;
    if(action==="test"){$("ai-settings-result").textContent="连接测试通过：短JSON请求已实际完成。点击保存后启用当前配置。";return;}
    identityRevision++;state.candidate=null;state.generationId=null;$("ai-candidate").hidden=true;
    if(typeof researchState!=="undefined"){researchState.version++;researchState.candidate=null;$("factor-candidate").textContent="AI配置已改变，候选可重新生成。";}
    clearAIInput();applyAIProfile(result.profile);
    $("ai-settings-result").textContent=action==="delete"?"个人配置已删除；当前AI状态以工作台默认配置为准。":"个人AI配置已保存。策略、因子候选和解释将使用此连接；保存未调用模型。";
  }catch(error){if(identity===identityRevision)$("ai-settings-result").textContent=error.message;}
  finally{aiSettingsBusy=false;for(const id of ["ai-save","ai-test","ai-delete","ai-settings-close","ai-provider","ai-base-url","ai-model","ai-api-key","ai-show-key"])$(id).disabled=false;$("ai-remember").disabled=!state.bootstrap.ai_remember_supported;}
}
function installAISettings(){
  const options=state.bootstrap.ai_providers.map(p=>`<option value="${escapeHtml(p.id)}">${escapeHtml(p.label)}</option>`).join("");
  $("ai-provider").innerHTML=options;$("auth-ai-provider").innerHTML='<option value="">暂不配置 / 保留现有配置</option>'+options;
  $("ai-settings-btn").addEventListener("click",()=>openAISettings());
  $("ai-provider").addEventListener("change",()=>chooseAIPreset($("ai-provider").value));
  $("ai-show-key").addEventListener("change",()=>{$("ai-api-key").type=$("ai-show-key").checked?"text":"password";});
  $("ai-settings-close").addEventListener("click",()=>{clearAIInput();$("ai-settings-dialog").close();});
  $("ai-settings-dialog").addEventListener("cancel",event=>{if(aiSettingsBusy)event.preventDefault();else clearAIInput();});
  $("ai-settings-form").addEventListener("submit",event=>{event.preventDefault();submitAISettings("save");});
  $("ai-test").addEventListener("click",()=>submitAISettings("test"));$("ai-delete").addEventListener("click",()=>submitAISettings("delete"));
  applyAIProfile(state.bootstrap.ai_profile);
}

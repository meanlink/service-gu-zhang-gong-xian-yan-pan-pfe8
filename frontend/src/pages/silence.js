import {datasets as datasetApi} from '../api/entities.js';
import {silence} from '../api/silence.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {esc, notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';

const DIRECTIONS=[['upstream','上游静默下游（沿调用链向下压）'],['downstream','下游静默上游（反向压制）']];
const SEVERITY_LABEL={1:'1 · 重度',2:'2 · 中度',3:'3 · 轻度'};
const REASON_TEXT={
 severe_always_visible:'重度告警，照常显示，不参与静默',
 no_visible_upstream_in_window:'时窗内没有实际可见的上游告警，保留显示',
 silenced_by_visible_upstream:'被时窗内实际可见的上游告警静默',
 replay_resumed:'中断恢复，复用已判定结论',
};

export async function silencePage(){
 const [plans,ds]=await Promise.all([silence.plans(),datasetApi.list()]);
 workspace.silencePlans=plans;workspace.datasets=ds;
 const selected=plans.find(p=>p.id===workspace.selectedSilencePlan)||plans[0];
 workspace.selectedSilencePlan=selected?.id??null;
 return heading('06 / ALERT SILENCE','选定批次回放静默','重度告警照常显示；轻度告警只认时窗内实际可见的上游告警为静默凭据，同刻不互相压制。分段接续已算时刻，任一服务告警全被压掉即拒绝批准。')
  +'<div class="silence-grid"><section class="panel"><div class="section-title"><h2>静默方案</h2><span>'+plans.length+' 个</span></div>'
  +'<div class="batch-list">'+(plans.length?plans.map(p=>'<button class="batch-item'+(p.id===workspace.selectedSilencePlan?' active':'')+'" data-plan="'+p.id+'"><strong>v'+p.params_version+' · '+esc(p.name)+'</strong><span>'+directionLabel(p.direction)+' · 时窗 '+esc(p.window_min)+' 分 · 段 '+p.segment_size+' 条</span><small>'+(p.approved?'当前版本已获准':'当前版本待重审')+'</small></button>').join(''):'<p class="empty">还没有静默方案。</p>')+'</div>'
  +planForm(ds,selected)
  +'</section><section class="panel" id="silence-detail">'+(selected?await planDetail(selected.id):'<div class="empty">选择或创建一个方案后开始分段回放。</div>')+'</section></div>';
}

function directionLabel(v){return (DIRECTIONS.find(d=>d[0]===v)||['',v])[1];}

function planForm(ds,selected){
 const canWrite=session.user.role!=='viewer';
 const p=selected||{name:'',dataset_id:ds[0]?.id,direction:'upstream',window_min:5,severe_levels:[1],segment_size:50,edges:[]};
 return '<div class="section-title" style="margin-top:26px"><h2>'+(selected?'修改方案（改动即升版重审）':'新建静默方案')+'</h2></div>'
  +'<form id="plan-form">'
  +'<label>方案名称<input name="name" required minlength="2" value="'+esc(p.name||'')+'" placeholder="例如：第45周值班静默方案" '+(selected?'disabled title="方案创建后名称不可改，修改口径会升版本"':'')+'></label>'
  +'<label>选定数据批次<select name="dataset_id" '+(selected?'disabled':'')+'>'+ds.map(d=>'<option value="'+d.id+'" '+(d.id===p.dataset_id?'selected':'')+'>'+esc(d.name)+' · '+d.rows+' 条</option>').join('')+'</select></label>'
  +'<label>静默方向<select name="direction">'+DIRECTIONS.map(d=>'<option value="'+d[0]+'" '+(d[0]===p.direction?'selected':'')+'>'+esc(d[1])+'</option>').join('')+'</select></label>'
  +'<div class="parameter-grid"><label>静默时窗(分钟)<input type="number" step="any" name="window_min" min="1" max="60" value="'+esc(p.window_min)+'" required></label>'
  +'<label>分段大小(条)<input type="number" step="1" name="segment_size" min="1" max="500" value="'+esc(p.segment_size)+'" required></label></div>'
  +'<div class="severity-pick"><span>重度等级（永远显示）</span>'+[1,2,3].map(s=>'<label class="check"><input type="checkbox" name="severe" value="'+s+'" '+(p.severe_levels.includes(s)?'checked':'')+'>'+SEVERITY_LABEL[s]+'</label>').join('')+'</div>'
  +edgesEditor(p.edges)
  +'<button type="submit" class="primary" '+(canWrite?'':'disabled')+'>'+(selected?'保存修改并升版本':'创建方案并选批回放 ↗')+'</button>'
  +(canWrite?'':'<p class="hint">观察员只有读取权限。</p>')+'</form>';
}

function edgesEditor(edges){
 const rows=(edges.length?edges:[['','']]).map(pairEditor).join('');
 return '<div class="edges-editor"><span>调用链（上游服务 → 下游服务，决定凭据方向）</span><div id="edge-rows">'+rows+'</div><button type="button" class="button small" id="add-edge">+ 增加一条调用链</button></div>';
}
function pairEditor(pair){
 return '<div class="edge-row"><input name="edge_up" placeholder="上游服务，如 cache" value="'+esc(pair[0]||'')+'"><span>→</span><input name="edge_down" placeholder="下游服务，如 api-gateway" value="'+esc(pair[1]||'')+'"></div>';
}

async function planDetail(planId){
 const plan=await silence.plan(planId);
 const replays=plan.replays||[];
 const current=replays.find(r=>r.params_version===plan.params_version)||replays[0];
 const briefs=await silence.briefs(planId).catch(()=>[]);
 let detail='<div class="section-title"><h2>'+esc(plan.name)+'</h2><span>版本 v'+plan.params_version+' · '+(plan.approved?'已获准':'待重审')+'</span></div>'
  +'<div class="plan-meta"><div><span>静默方向</span><strong>'+esc(directionLabel(plan.direction))+'</strong></div><div><span>时窗</span><strong>'+esc(plan.window_min)+' 分钟</strong></div><div><span>重度等级</span><strong>'+plan.severe_levels.map(s=>SEVERITY_LABEL[s]||s).join('、')+'</strong></div><div><span>分段</span><strong>'+plan.segment_size+' 条/段</strong></div></div>'
  +'<div class="method-sheet compact"><p class="eyebrow">REPLAY / SEGMENTED</p><p>每段都接着上一段已判定的“实际可见告警”继续计算；中断恢复后沿已算时刻接续，重试同段按事件 ID 去重，不重复累计。</p></div>';
 if(current){
  const full=await silence.replay(current.id);
  detail+=replayPanel(plan,full,briefs);
 }else{
  detail+='<div class="empty">当前版本还没有回放。<button class="primary" id="start-replay" '+(session.user.role==='viewer'?'disabled':'')+'>发起分段回放 ↗</button></div>';
 }
 return detail;
}

function replayPanel(plan,replay,briefs){
 const stats=replay.service_stats||[];
 const decisions=replay.decisions||[];
 const reasons=(replay.approval&&replay.approval.reasons)||[];
 const progress=replay.total_events?Math.round(replay.processed_events/replay.total_events*100):0;
 const canWrite=session.user.role!=='viewer';
 const isAdmin=session.user.role==='admin';
 let html='<div class="replay-head"><div><h3>回放 #'+replay.id+' · v'+replay.params_version+'</h3><p>'+(replay.status==='completed'?'已覆盖全部 '+replay.total_events+' 条告警':'进行中：第 '+replay.processed_segments+'/'+replay.total_segments+' 段，已算 '+replay.processed_events+' 条')+'</p></div><span class="status '+replay.status+'">'+(replay.status==='completed'?'回放完成':'分段进行中')+'</span></div>'
  +'<div class="progress"><i style="width:'+progress+'%"></i></div>'
  +'<div class="replay-actions">'
  +'<button class="button small" data-action="start" data-replay="'+replay.id+'" '+(canWrite?'':'disabled')+'>新回放</button>'
  +'<button class="button small" data-action="advance" data-replay="'+replay.id+'" '+(canWrite?'':'disabled')+'>推进下一段 ↻</button>'
  +'<button class="button small" data-action="runend" data-replay="'+replay.id+'" '+(canWrite?'':'disabled')+'>连续回放至完成</button>'
  +(replay.status==='completed'?'<button class="primary small" data-action="approve" data-replay="'+replay.id+'" '+(isAdmin&&!plan.approved?'':'disabled')+'>提交负责人批准</button>':'')
  +'</div>';
 if(reasons.length){
  html+='<div class="reject-box"><p class="eyebrow">批准被拒原因</p>'+reasons.map(r=>'<p class="warning">['+esc(r.code)+']'+(r.service?' 服务 '+esc(r.service):'')+' · '+esc(r.message)+'</p>').join('')+'</div>';
 }
 if(stats.length){
  html+='<div class="service-stats">'+stats.map(s=>'<div class="'+(s.visible===0?'blind':'')+'"><span>'+esc(s.service)+'</span><strong>'+s.visible+'/'+s.total+' 可见</strong><small>静默 '+s.silenced+' 条 · 重度 '+s.severe+' 条</small>'+(s.visible===0?'<em>整条链失明风险</em>':'')+'</div>').join('')+'</div>';
 }
 if(decisions.length){
  html+='<div class="section-title" style="margin-top:24px"><h2>逐条静默凭据</h2><span>'+decisions.length+' 条</span></div>'
   +'<div class="table-wrap"><table><thead><tr><th>事件ID</th><th>服务</th><th>时刻(分)</th><th>等级</th><th>结论</th><th>静默凭据</th><th>凭据服务</th><th>时滞(分)</th><th>判定原因</th></tr></thead><tbody>'
   +decisions.map(d=>'<tr class="'+(d.silenced?'row-silenced':'row-visible')+'"><td>'+esc(d.event_id)+'</td><td>'+esc(d.service)+'</td><td>'+esc(d.occurred_min)+'</td><td>'+esc(d.severity)+'</td><td>'+(d.silenced?'<span class="tag muted">已静默</span>':'<span class="tag live">可见</span>')+'</td><td>'+esc(d.credential_event_id||'—')+'</td><td>'+esc(d.credential_service||'—')+'</td><td>'+esc(d.lag_min??'—')+'</td><td>'+esc(REASON_TEXT[d.reason]||d.reason)+'</td></tr>').join('')
   +'</tbody></table></div>';
 }
 html+='<div class="section-title" style="margin-top:24px"><h2>静默简报（冻结获准快照）</h2><span>'+briefs.length+' 份</span></div>';
 if(plan.approved&&isAdmin){
  html+='<form id="brief-form" class="inline-form"><input name="name" required minlength="2" placeholder="简报名称，如：值班静默简报 · 第1期" value="值班静默简报 · v'+plan.params_version+'"><button class="primary small" type="submit">生成简报（只收当前获准回放）</button></form>';
 }else if(!plan.approved){
  html+='<p class="hint">当前版本未获准，不能生成或分发简报；修改与分发并发时，已发简报保留原依据。</p>';
 }
 html+='<div class="brief-list">'+briefs.map(b=>'<article><div><strong>'+esc(b.name)+'</strong><p>基于 v'+b.params_version+' · 回放 #'+b.replay_id+'</p><small>SHA256 '+esc(b.basis_checksum.slice(0,24))+'… · 静默 '+b.snapshot.replay.summary.silenced_alerts+'/'+b.snapshot.replay.summary.total_alerts+' 条</small></div></article>').join('')+'</div>';
 return html;
}

export function bindSilence(refresh){
 document.querySelectorAll('[data-plan]').forEach(b=>b.onclick=()=>{workspace.selectedSilencePlan=Number(b.dataset.plan);refresh();});
 const form=document.querySelector('#plan-form');
 if(form)submit(form,async f=>{
  const edges=[...document.querySelectorAll('#edge-rows .edge-row')].map(row=>{const ins=row.querySelectorAll('input');return{upstream:ins[0].value.trim(),downstream:ins[1].value.trim()};}).filter(e=>e.upstream&&e.downstream);
  const severe=[...f.getAll('severe')].map(Number);
  if(!severe.length)throw new Error('至少勾选一个重度等级（重度告警永远显示）');
  const payload={direction:f.get('direction'),window_min:Number(f.get('window_min')),segment_size:Number(f.get('segment_size')),severe_levels:severe,edges};
  const editing=workspace.selectedSilencePlan;
  if(editing){await silence.updatePlan(editing,payload);notify('方案已修改并升版本，批准已撤销，须重审');}
  else{payload.name=f.get('name');payload.dataset_id=Number(f.get('dataset_id'));const p=await silence.createPlan(payload);workspace.selectedSilencePlan=p.id;notify('方案已创建，可发起分段回放');}
  await refresh();
 });
 document.querySelector('#add-edge')?.addEventListener('click',()=>{const wrap=document.querySelector('#edge-rows');wrap.insertAdjacentHTML('beforeend',pairEditor(['','']));});
 const planId=workspace.selectedSilencePlan;
 document.querySelector('#start-replay')?.addEventListener('click',async()=>{const r=await silence.startReplay(planId);notify('已发起回放 #'+r.id);await refresh();});
 document.querySelector('[data-action="start"]')?.addEventListener('click',async()=>{const r=await silence.startReplay(planId);notify('已发起回放 #'+r.id);await refresh();});
 document.querySelector('[data-action="advance"]')?.addEventListener('click',async(e)=>{const id=Number(e.currentTarget.dataset.replay);const r=await silence.advance(id);notify(r.status==='completed'?'回放已覆盖全部告警（重复推进幂等）':'已推进到第 '+r.processed_segments+' 段');await refresh();});
 document.querySelector('[data-action="runend"]')?.addEventListener('click',async(e)=>{const id=Number(e.currentTarget.dataset.replay);await silence.runToEnd(id);notify('回放已覆盖全部告警');await refresh();});
 document.querySelector('[data-action="approve"]')?.addEventListener('click',async(e)=>{const id=Number(e.currentTarget.dataset.replay);try{await silence.approve(id);notify('已批准，可生成静默简报');}catch(err){notify('批准被拒：'+err.message,true);}await refresh();});
 const briefForm=document.querySelector('#brief-form');
 if(briefForm)submit(briefForm,async f=>{await silence.createBrief(planId,{name:f.get('name')});notify('简报已按当前获准回放冻结快照');await refresh();});
}

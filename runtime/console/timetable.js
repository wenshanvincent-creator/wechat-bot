function renderTimetable(out){
  const data=state.timetable||{entries:[],settings:{}};
  const cfg=data.settings||{};
  const days=['','周一','周二','周三','周四','周五','周六','周日'];
  const rows=(data.entries||[]).map(e=>
    `<tr><td>${days[e.day]||''}</td><td>${e.period?'第'+e.period+'节':''}</td>`+
    `<td>${esc(e.start)}–${esc(e.end)}</td><td>${esc(e.subject)}</td></tr>`).join('');
  out.insertAdjacentHTML('beforeend',
    `<div class="panel"><h2>课程表</h2>`+
    `<p>${cfg.enabled?'课前提醒已开启':'课前提醒未开启'} · 提前 ${cfg.lead_minutes||5} 分钟 · 发到 ${esc(name('channels',cfg.channel_id)||'未选择')}</p>`+
    `<p><button class="secondary" id="tt-toggle">${cfg.enabled?'关闭课前提醒':'开启课前提醒'}</button> `+
    `提前 <input id="tt-lead" type="number" min="1" max="120" value="${cfg.lead_minutes||5}" style="width:80px"> 分钟 `+
    `<select id="tt-channel">${state.channels.map(c=>`<option value="${esc(c.id)}" ${c.id===cfg.channel_id?'selected':''}>${esc(c.name)}</option>`).join('')}</select> `+
    `<button class="secondary" id="tt-save">保存设置</button></p>`+
    `<table><thead><tr><th>星期</th><th>节次</th><th>时间</th><th>课程</th></tr></thead>`+
    `<tbody>${rows||'<tr><td colspan="4">还没有课程表</td></tr>'}</tbody></table>`+
    `<label>粘贴课程表（每行一节，例如“周一 08:00-08:40 TOK 209”）<textarea id="tt-text" rows="6"></textarea></label>`+
    `<button id="tt-import">导入/替换课程表</button></div>`);
  $('#tt-toggle').onclick=async()=>{try{await api('timetable/settings',{enabled:!cfg.enabled});notice('已保存');await refresh()}catch(e){notice(e.message)}};
  $('#tt-save').onclick=async()=>{try{await api('timetable/settings',{lead_minutes:Number($('#tt-lead').value),channel_id:$('#tt-channel').value});notice('已保存');await refresh()}catch(e){notice(e.message)}};
  $('#tt-import').onclick=async()=>{try{const r=await api('timetable/import',{text:$('#tt-text').value});notice('导入 '+r.count+' 节');await refresh()}catch(e){notice(e.message)}};
}

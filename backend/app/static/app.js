(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const state = {
    tasks: [], currentTask: null, eventSource: null,
    model: null, barsByIndex: new Map(), robotWaypoints: new Map(), assemblyPaths: new Map(),
    step: 0, alpha: 0, playing: false, speed: 1,
    lastFrame: performance.now(), yaw: -0.78, pitch: 0.42, zoom: 1,
    dragging: false, lastX: 0, lastY: 0, dragStartX: 0, dragStartY: 0,
    center: [0,0,0], span: 1, baseScale: 1,
    visualEditorActive: false, visualOrder: [], visualPreinstalled: new Set(),
    visualSelected: null, visualBarPayload: null, meshGroupPayload: null, visualSourceKey: null,
    visualDragIndex: null, visualSelection: new Set(),
    visualBoxMode: false, visualBoxStart: null, visualBoxCurrent: null,
    visualMode: 'bars', groupPaths: new Map(),
    meshGroups: [], meshDraftSelection: new Set(), meshEditingGroupId: null,
    meshSelectedGroupId: null, meshGroupSerial: 1, meshDragIndex: null,
    meshStage: 'grouping', meshResolved: null, meshPreviewAlpha: 0, meshRevision: 0,
    loadedResultTaskId: null,
    meshDefaults: {longitudinalAxis:[1,0,0],verticalAxis:[0,0,1],topElevation:null,clearance:800},
  };

  const meshPalette=['#54a7ff','#ff9d45','#60d394','#c187ff','#ff6f91','#69e6ff','#ffd166','#a8dadc','#f28482','#90be6d','#b8c0ff','#f6bd60'];

  const canvas = $('viewerCanvas');
  const ctx = canvas.getContext('2d', {alpha: true});
  let dpr = Math.max(1, Math.min(2, window.devicePixelRatio || 1));

  async function api(url, options = {}) {
    const response = await fetch(url, options);
    if (!response.ok) {
      let detail = response.statusText;
      try { const data = await response.json(); detail = data.detail || JSON.stringify(data); } catch (_) {}
      throw new Error(detail);
    }
    const type = response.headers.get('content-type') || '';
    return type.includes('application/json') ? response.json() : response;
  }

  function updateSequenceGeneratorState() {
    const button = $('generateSequenceBtn');
    if (button) button.disabled = !$('fileInput').files?.[0];
    const visualButton = $('visualSequenceBtn');
    if (visualButton) visualButton.disabled = !$('fileInput').files?.[0];
    const meshButton = $('meshGroupBtn');
    if (meshButton) meshButton.disabled = !$('fileInput').files?.[0];
  }

  function selectedFileKey() {
    const file=$('fileInput').files?.[0];
    return file?[file.name,file.size,file.lastModified].join(':'):null;
  }

  function resetVisualSequence() {
    state.visualEditorActive=false;
    state.visualOrder=[];
    state.visualPreinstalled=new Set();
    state.visualSelected=null;
    state.visualSelection=new Set();
    state.visualBarPayload=null;state.meshGroupPayload=null;
    state.visualSourceKey=null;
    state.visualMode='bars';state.groupPaths=new Map();
    state.meshGroups=[];state.meshDraftSelection=new Set();state.meshEditingGroupId=null;
    state.meshSelectedGroupId=null;state.meshGroupSerial=1;state.meshDragIndex=null;
    state.meshStage='grouping';state.meshResolved=null;state.meshPreviewAlpha=0;state.meshRevision++;
    state.meshDefaults={longitudinalAxis:[1,0,0],verticalAxis:[0,0,1],topElevation:null,clearance:800};
    for(const id of ['meshLongitudinalX','meshLongitudinalY','meshLongitudinalZ']){$(id).value='';$(id).placeholder='自动';}
    $('meshTopElevation').value='';$('meshTopElevation').placeholder='自动识别';
    $('meshDefaultClearance').value='800';
    setVisualBoxMode(false);
    $('visualSequenceEditor').classList.add('hidden');
    $('viewerPlayer').classList.remove('hidden');
    $('visualSequenceSummary').textContent='选择 IFC 后，可在三维模型中点击钢筋并指定安装顺序。';
    $('meshGroupSummary').textContent='点选或框选钢筋组成网片组，再指定各网片组的安装顺序与旋转参数。';
  }

  async function generateSequenceWorkbook() {
    const file = $('fileInput').files[0];
    if (!file) { showNote('请先选择 IFC 文件。', true); return; }
    const button = $('generateSequenceBtn');
    const form = new FormData();
    form.append('file', file);
    button.disabled = true;
    showNote('正在解析 IFC 并生成 Excel…');
    try {
      const response = await fetch('/api/sequence/generate', { method: 'POST', body: form });
      if (!response.ok) {
        let detail = response.statusText;
        try { const payload = await response.json(); detail = payload.detail || detail; } catch (_) {}
        throw new Error(detail);
      }
      const blob = await response.blob();
      const disposition = response.headers.get('content-disposition') || '';
      const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
      const plain = disposition.match(/filename="?([^";]+)"?/i)?.[1];
      let filename = 'rebar_installation_sequence.xlsx';
      if (encoded) filename = decodeURIComponent(encoded);
      else if (plain) filename = plain;
      const href = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = href;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(href), 1000);
      showNote(`已生成：${filename}；调整 name 列行顺序后可直接上传。`);
    } catch (err) {
      showNote(err.message, true);
    } finally {
      updateSequenceGeneratorState();
    }
  }

  function visualBarTitle(bar) {
    return bar?.n || ('钢筋索引 ' + bar?.i);
  }

  function visualBarDetails(bar) {
    const name=bar?.name&&bar.name!==bar.n?bar.name:'';
    return ('索引 ' + bar.i + ' · 直径 ' + (2*bar.r).toFixed(1) + ' mm' + (name?' · '+name:''));
  }

  function setVisualBoxMode(enabled) {
    state.visualBoxMode=Boolean(enabled)&&state.visualEditorActive;
    state.visualBoxStart=null;state.visualBoxCurrent=null;
    const button=$('visualBoxSelectBtn');
    button.classList.toggle('active',state.visualBoxMode);
    button.setAttribute('aria-pressed',String(state.visualBoxMode));
    button.textContent=state.visualBoxMode?'框选中（拖动）':'框选钢筋';
    canvas.classList.toggle('box-select-mode',state.visualBoxMode);
    if(state.model)draw();
  }

  function canvasPointFromEvent(event) {
    const rect=canvas.getBoundingClientRect();
    return [
      (event.clientX-rect.left)*(canvas.width/rect.width),
      (event.clientY-rect.top)*(canvas.height/rect.height),
    ];
  }

  function showVisualEditor() {
    if(!state.model?.bars?.length||state.visualSourceKey!==selectedFileKey())return;
    state.visualEditorActive=true;
    configureVisualEditorMode();
    $('visualSequenceEditor').classList.remove('hidden');
    $('viewerPlayer').classList.add('hidden');
    $('emptyState').classList.add('hidden');
    renderVisualSequenceEditor();
    draw();
    document.querySelector('.viewer-panel')?.scrollIntoView({behavior:'smooth',block:'start'});
  }

  async function loadVisualSequencePreview() {
    const file=$('fileInput').files[0];
    if(!file){showNote('请先选择 IFC 文件。',true);return;}
    if(state.visualSourceKey===selectedFileKey()&&state.model?.source_filename===file.name){
      state.visualMode='bars';configureVisualEditorMode();showVisualEditor();return;
    }
    const button=$('visualSequenceBtn'),form=new FormData();
    form.append('file',file);button.disabled=true;
    showNote('正在解析 IFC 并生成可点击的三维模型…');
    try{
      const model=await api('/api/sequence/preview',{method:'POST',body:form});
      if(state.eventSource){state.eventSource.close();state.eventSource=null;}
      state.currentTask=null;renderTaskList();
      state.model=model;
      state.barsByIndex=new Map(model.bars.map(bar=>[bar.i,bar]));
      state.assemblyPaths=new Map();state.robotWaypoints=new Map();
      state.step=0;state.alpha=0;state.playing=false;
      state.visualOrder=[];state.visualPreinstalled=new Set();state.visualSelected=null;
      state.visualSelection=new Set();state.visualBoxMode=false;
      state.visualBoxStart=null;state.visualBoxCurrent=null;
      state.visualBarPayload=null;state.meshGroupPayload=null;state.visualSourceKey=selectedFileKey();
      state.visualEditorActive=true;
      state.visualMode='bars';
      configureVisualEditorMode();
      setVisualBoxMode(false);
      $('visualSequenceEditor').classList.remove('hidden');
      $('viewerPlayer').classList.add('hidden');
      $('emptyState').classList.add('hidden');
      $('metricBars').textContent=model.bars.length.toLocaleString('zh-CN');
      $('metricTypes').textContent=(model.meta?.axis_type_count||0).toLocaleString('zh-CN');
      $('metricLength').textContent=(model.meta?.axis_total_length_m||0).toFixed(1)+' m';
      $('metricFeasible').textContent='待指定顺序';
      $('metricFeasible').style.color='var(--orange)';
      computeBounds();fitView();renderVisualSequenceEditor();
      $('visualSequenceSummary').textContent='已解析 '+model.bars.length.toLocaleString('zh-CN')+' 根钢筋，等待指定完整顺序。';
      showNote('模型已加载：点击三维钢筋即可按安装先后依次加入。');
    }catch(err){showNote(err.message,true);}
    finally{updateSequenceGeneratorState();}
  }

  function renderVisualSequenceEditor() {
    if(state.visualMode==='groups'){renderMeshGroupEditor();return;}
    const bars=state.model?.bars||[],orderedSet=new Set(state.visualOrder);
    const term=$('visualSequenceSearch').value.trim().toLowerCase();
    const available=bars.filter(bar=>{
      if(orderedSet.has(bar.i))return false;
      if(!term)return true;
      return [bar.i,bar.n,bar.name,bar.tag].some(value=>String(value??'').toLowerCase().includes(term));
    });
    $('visualAvailableCount').textContent=(bars.length-state.visualOrder.length).toLocaleString('zh-CN');
    $('visualOrderedCount').textContent=state.visualOrder.length.toLocaleString('zh-CN');
    $('visualEditorProgress').textContent='已排序 '+state.visualOrder.length.toLocaleString('zh-CN')+' / '+bars.length.toLocaleString('zh-CN');
    const shown=available.slice(0,300);
    $('visualAvailableList').innerHTML=shown.length?shown.map(bar=>
      '<button type=\"button\" class=\"visual-candidate\" data-add=\"'+bar.i+'\">'+
      '<span class=\"visual-bar-text\"><strong>'+escapeHtml(visualBarTitle(bar))+'</strong>'+
      '<small>'+escapeHtml(visualBarDetails(bar))+'</small></span><span class=\"visual-add\">＋</span></button>'
    ).join(''):'<div class=\"visual-sequence-empty\">没有符合条件的未排序钢筋</div>';
    if(available.length>shown.length){
      $('visualAvailableList').insertAdjacentHTML('beforeend','<div class=\"visual-sequence-empty\">还有 '+(available.length-shown.length).toLocaleString('zh-CN')+' 根，请输入 BIM ID 缩小范围</div>');
    }
    $('visualOrderedList').innerHTML=state.visualOrder.length?state.visualOrder.map((barIndex,index)=>{
      const bar=state.barsByIndex.get(barIndex),selected=state.visualSelection.has(barIndex)?' selected':'';
      const checked=state.visualPreinstalled.has(barIndex)?' checked':'';
      return '<div class=\"visual-order-row'+selected+'\" draggable=\"true\" data-order-index=\"'+index+'\" data-bar-index=\"'+barIndex+'\">'+
        '<span class=\"visual-order-number\">'+(index+1)+'</span>'+
        '<span class=\"visual-bar-text\"><strong>'+escapeHtml(visualBarTitle(bar))+'</strong><small>'+escapeHtml(visualBarDetails(bar))+'</small></span>'+
        '<label class=\"visual-installed\"><input type=\"checkbox\" data-installed=\"'+barIndex+'\"'+checked+'> 已安装</label>'+
        '<button type=\"button\" data-move=\"-1\" title=\"上移\">↑</button>'+
        '<button type=\"button\" data-move=\"1\" title=\"下移\">↓</button>'+
        '<button type=\"button\" class=\"remove\" data-remove=\"'+barIndex+'\" title=\"移除\">×</button></div>';
    }).join(''):'<div class=\"visual-sequence-empty\">点击三维钢筋或左侧“＋”开始排序</div>';
  }

  function addVisualBar(barIndex) {
    if(!state.barsByIndex.has(barIndex))return;
    if(!state.visualOrder.includes(barIndex))state.visualOrder.push(barIndex);
    state.visualSelected=barIndex;state.visualSelection=new Set([barIndex]);state.visualBarPayload=null;
    renderVisualSequenceEditor();draw();
  }

  function moveVisualBar(from,to) {
    if(from<0||from>=state.visualOrder.length)return;
    to=Math.max(0,Math.min(state.visualOrder.length-1,to));
    if(from===to)return;
    const [barIndex]=state.visualOrder.splice(from,1);
    state.visualOrder.splice(to,0,barIndex);
    state.visualSelected=barIndex;state.visualSelection=new Set([barIndex]);state.visualBarPayload=null;
    renderVisualSequenceEditor();draw();
  }

  function fillVisualOrder() {
    const assigned=new Set(state.visualOrder);
    for(const bar of state.model?.bars||[])if(!assigned.has(bar.i))state.visualOrder.push(bar.i);
    state.visualBarPayload=null;renderVisualSequenceEditor();draw();
  }

  function clearVisualOrder() {
    state.visualOrder=[];state.visualPreinstalled=new Set();state.visualSelected=null;
    state.visualSelection=new Set();
    state.visualBarPayload=null;renderVisualSequenceEditor();draw();
  }

  function saveVisualOrder() {
    const total=state.model?.bars?.length||0;
    if(!total||state.visualOrder.length!==total){
      showNote('顺序尚不完整：还需加入 '+(total-state.visualOrder.length)+' 根钢筋。',true);return;
    }
    state.visualBarPayload={items:state.visualOrder.map((barIndex,index)=>({
      installation_step:index+1,
      bar_index:barIndex,
      installation_status:state.visualPreinstalled.has(barIndex)?'preinstalled':'pending',
    }))};
    state.visualEditorActive=false;
    setVisualBoxMode(false);
    $('visualSequenceEditor').classList.add('hidden');
    $('viewerPlayer').classList.remove('hidden');
    const installed=state.visualPreinstalled.size;
    $('visualSequenceSummary').textContent='顺序已保存：共 '+total+' 根，其中 '+installed+' 根标记为已安装。';
    showNote('可视化人工顺序已保存，可以开始计算。');
    draw();$('viewerInfo').textContent='可视化人工顺序已保存 · '+total+' 根钢筋';
  }

  function closeVisualEditor() {
    state.visualEditorActive=false;
    setVisualBoxMode(false);
    $('visualSequenceEditor').classList.add('hidden');
    $('viewerPlayer').classList.remove('hidden');
    draw();
  }

  function nullableNumber(value) {
    if(value===null||value===undefined||String(value).trim()==='')return null;
    const parsed=Number(value);return Number.isFinite(parsed)?parsed:null;
  }

  function invalidateMeshSolution() {
    state.meshResolved=null;state.meshGroupPayload=null;state.meshRevision++;
    for(const group of state.meshGroups){delete group.resolved_values;delete group.plane_fit;}
  }

  function configureVisualEditorMode() {
    const groups=state.visualMode==='groups';
    $('visualSingleEditor').classList.toggle('hidden',groups);
    $('meshGroupEditor').classList.toggle('hidden',!groups);
    $('visualFillBtn').classList.toggle('hidden',groups);
    $('visualEditorEyebrow').textContent=groups?'MESH GROUP ASSEMBLY EDITOR':'VISUAL SEQUENCE EDITOR';
    $('visualEditorTitle').textContent=groups?'可视化划分钢筋网片组并指定安装顺序':'可视化指定钢筋安装顺序';
    $('visualEditorDescription').textContent=groups
      ?'点选或框选钢筋形成网片草稿，确认分组后设置整组安装顺序、平面角度和纵向旋转轴。'
      :'可单击钢筋逐根加入，也可开启框选后拖出矩形，一次加入一根或多根钢筋。';
    $('visualClearBtn').textContent=groups?'清空全部网片':'清空';
    $('visualSaveBtn').textContent=groups?'保存网片组顺序':'保存人工顺序';
    if(groups)setMeshStage(state.meshStage||'grouping');
  }

  function meshAssignedMap(ignoreGroupId=null) {
    const result=new Map();
    for(const group of state.meshGroups){
      if(group.group_id===ignoreGroupId)continue;
      for(const index of group.bar_indices)result.set(index,group.group_id);
    }
    return result;
  }

  function meshGroupById(groupId) {
    return state.meshGroups.find(group=>group.group_id===groupId)||null;
  }

  function setMeshStage(stage) {
    if(stage!=='grouping'&&(state.meshEditingGroupId||state.meshDraftSelection.size)){
      showNote('当前网片草稿尚未确认，请先保存网片组修改或取消草稿。',true);
      return;
    }
    state.meshStage=stage;
    document.querySelectorAll('[data-mesh-stage]').forEach(button=>button.classList.toggle('active',button.dataset.meshStage===stage));
    $('meshGroupingStage').classList.toggle('hidden',stage!=='grouping');
    $('meshParametersStage').classList.toggle('hidden',stage!=='parameters');
    $('meshPreviewStage').classList.toggle('hidden',stage!=='preview');
    if(stage==='preview')renderMeshPreviewDetails();
    draw();
  }

  function resetMeshDraft() {
    state.meshDraftSelection=new Set();state.meshEditingGroupId=null;
    $('meshDraftName').value='';
  }

  function renderMeshGroupEditor() {
    if(!state.model)return;
    configureVisualEditorMode();
    const bars=state.model.bars||[];
    const assigned=meshAssignedMap(state.meshEditingGroupId);
    const term=$('meshGroupSearch').value.trim().toLowerCase();
    const available=bars.filter(bar=>{
      if(assigned.has(bar.i)||state.meshDraftSelection.has(bar.i))return false;
      return !term||[bar.i,bar.n,bar.name,bar.tag].some(value=>String(value??'').toLowerCase().includes(term));
    });
    const assignedCount=new Set(state.meshGroups.flatMap(group=>group.bar_indices)).size;
    $('meshUnassignedCount').textContent=(bars.length-assignedCount-(state.meshEditingGroupId?0:state.meshDraftSelection.size)).toLocaleString('zh-CN');
    $('meshDraftCount').textContent=state.meshDraftSelection.size.toLocaleString('zh-CN');
    $('meshGroupCount').textContent=state.meshGroups.length.toLocaleString('zh-CN');
    $('visualEditorProgress').textContent=`已分组 ${assignedCount} / ${bars.length} · ${state.meshGroups.length} 个网片组`;
    $('visualEditorHint').textContent=state.meshEditingGroupId
      ?'正在编辑已有网片；确认前不会静默移动其他组的钢筋。'
      :'点选或框选只会加入未分组钢筋；如需跨组移动，请使用“重新分组”。';
    const shown=available.slice(0,300);
    $('meshUnassignedList').innerHTML=shown.length?shown.map(bar=>
      `<button type="button" class="visual-candidate" data-mesh-add="${bar.i}"><span class="visual-bar-text"><strong>${escapeHtml(visualBarTitle(bar))}</strong><small>${escapeHtml(visualBarDetails(bar))}</small></span><span class="visual-add">＋</span></button>`
    ).join(''):'<div class="visual-sequence-empty">没有符合条件的未分组钢筋</div>';
    const draft=[...state.meshDraftSelection].sort((a,b)=>a-b);
    $('meshDraftList').innerHTML=draft.length?draft.map(index=>{
      const bar=state.barsByIndex.get(index);
      return `<div class="mesh-member-row"><span class="visual-bar-text"><strong>${escapeHtml(visualBarTitle(bar))}</strong><small>${escapeHtml(visualBarDetails(bar))}</small></span><button type="button" data-mesh-draft-remove="${index}" title="移出草稿">×</button></div>`;
    }).join(''):'<div class="visual-sequence-empty">在三维视图点选或框选钢筋</div>';
    $('meshCancelDraftBtn').classList.toggle('hidden',!state.meshEditingGroupId&&!draft.length);
    $('meshConfirmGroupBtn').textContent=state.meshEditingGroupId?'保存网片组修改':'确认网片组';
    $('meshGroupList').innerHTML=state.meshGroups.length?state.meshGroups.map((group,index)=>{
      const selected=group.group_id===state.meshSelectedGroupId?' selected':'';
      return `<article class="mesh-group-card${selected}" data-mesh-group="${escapeHtml(group.group_id)}" style="--group-color:${group.color}"><i></i><div><strong>${escapeHtml(group.name)}</strong><small>${group.bar_indices.length} 根 · 顺序 ${index+1}${group.installation_status==='preinstalled'?' · 已安装':''}</small></div><button type="button" data-mesh-edit="${escapeHtml(group.group_id)}">编辑</button><button type="button" class="remove" data-mesh-delete="${escapeHtml(group.group_id)}">删除</button></article>`;
    }).join(''):'<div class="visual-sequence-empty">尚未创建网片组；常见箱梁可按八个纵向面划分</div>';
    renderMeshParameterList();
  }

  function renderMeshParameterList() {
    $('meshParameterGroupCount').textContent=`${state.meshGroups.length} 组`;
    $('meshParameterList').innerHTML=state.meshGroups.length?state.meshGroups.map((group,index)=>{
      const automatic=group.resolved_values||{};
      const angle=group.plane_angle_deg??automatic.plane_angle_deg??'';
      const transverse=group.rotation_axis?.transverse_mm??automatic.axis_transverse_mm??'';
      const elevation=group.rotation_axis?.elevation_mm??automatic.axis_elevation_mm??'';
      const clearance=group.staging_clearance_mm??automatic.staging_clearance_mm??'';
      return `<article class="mesh-parameter-card" draggable="true" data-mesh-order-index="${index}" data-mesh-group-id="${escapeHtml(group.group_id)}" style="--group-color:${group.color}">
        <header><i></i><div><strong><span>${index+1}</span> ${escapeHtml(group.name)}</strong><small>${group.bar_indices.length} 根钢筋</small></div><label><input type="checkbox" data-mesh-field="installation_status" ${group.installation_status==='preinstalled'?'checked':''}> 整组已安装</label><button type="button" data-mesh-move="-1">↑</button><button type="button" data-mesh-move="1">↓</button></header>
        <div class="mesh-parameter-grid"><label>最终平面角 / °<input type="number" min="-180" max="180" step="0.1" data-mesh-field="plane_angle_deg" value="${angle}"></label><label>旋转轴横向 / mm<input type="number" step="1" data-mesh-field="axis_transverse" value="${transverse}"></label><label>旋转轴标高 / mm<input type="number" step="1" data-mesh-field="axis_elevation" value="${elevation}"></label><label>初始抬高 / mm<input type="number" min="0" step="10" data-mesh-field="staging_clearance_mm" value="${clearance}" placeholder="默认 ${state.meshDefaults.clearance}"></label></div>
      </article>`;
    }).join(''):'<div class="visual-sequence-empty">请先完成网片分组</div>';
  }

  function addMeshDraftBar(index) {
    const assigned=meshAssignedMap(state.meshEditingGroupId);
    if(assigned.has(index)){
      state.meshSelectedGroupId=assigned.get(index);
      state.visualSelection=new Set([index]);
      showNote('该钢筋已属于其他网片组；请选中后使用“重新分组”。',true);renderMeshGroupEditor();draw();return;
    }
    if(state.meshDraftSelection.has(index))state.meshDraftSelection.delete(index);else state.meshDraftSelection.add(index);
    state.visualSelection=new Set(state.meshDraftSelection);invalidateMeshSolution();
    renderMeshGroupEditor();draw();
  }

  function confirmMeshGroup() {
    const indices=[...state.meshDraftSelection].sort((a,b)=>a-b);
    if(!indices.length){showNote('请先点选或框选至少一根钢筋。',true);return;}
    const name=$('meshDraftName').value.trim()||`网片组 ${state.meshGroupSerial}`;
    const conflict=meshAssignedMap(state.meshEditingGroupId);
    const blocked=indices.find(index=>conflict.has(index));
    if(blocked!==undefined){showNote(`钢筋 ${blocked} 已属于其他网片组，不能静默转移。`,true);return;}
    if(state.meshEditingGroupId){
      const group=meshGroupById(state.meshEditingGroupId);if(!group)return;
      group.name=name;group.bar_indices=indices;
    }else{
      const serial=state.meshGroupSerial++;
      state.meshGroups.push({group_id:`G${String(serial).padStart(3,'0')}`,name,bar_indices:indices,color:meshPalette[(serial-1)%meshPalette.length],installation_status:'pending',plane_angle_deg:null,rotation_axis:{transverse_mm:null,elevation_mm:null,direction:null},staging_clearance_mm:null});
    }
    resetMeshDraft();state.visualSelection=new Set();invalidateMeshSolution();
    renderMeshGroupEditor();draw();showNote(`已保存网片组“${name}”。`);
  }

  function editMeshGroup(groupId) {
    const group=meshGroupById(groupId);if(!group)return;
    state.meshEditingGroupId=groupId;state.meshSelectedGroupId=groupId;
    state.meshDraftSelection=new Set(group.bar_indices);state.visualSelection=new Set(group.bar_indices);
    $('meshDraftName').value=group.name;setMeshStage('grouping');renderMeshGroupEditor();draw();
  }

  function deleteMeshGroup(groupId) {
    const group=meshGroupById(groupId);if(!group)return;
    state.meshGroups=state.meshGroups.filter(item=>item.group_id!==groupId);
    if(state.meshEditingGroupId===groupId)resetMeshDraft();
    if(state.meshSelectedGroupId===groupId)state.meshSelectedGroupId=null;
    invalidateMeshSolution();renderMeshGroupEditor();draw();
    showNote(`已删除“${group.name}”，其中钢筋恢复为未分组。`);
  }

  function reassignSelectedMeshBars() {
    const selected=[...state.visualSelection];if(!selected.length){showNote('请先在三维视图选择需要重新分组的钢筋。',true);return;}
    for(const index of selected){
      for(const group of state.meshGroups)group.bar_indices=group.bar_indices.filter(value=>value!==index);
      state.meshDraftSelection.add(index);
    }
    state.meshGroups=state.meshGroups.filter(group=>group.bar_indices.length);
    state.meshEditingGroupId=null;$('meshDraftName').value='';invalidateMeshSolution();
    renderMeshGroupEditor();draw();showNote(`已将 ${selected.length} 根钢筋移入当前草稿，请确认新的网片组。`);
  }

  function moveMeshGroup(from,to) {
    if(from<0||from>=state.meshGroups.length)return;
    to=Math.max(0,Math.min(state.meshGroups.length-1,to));if(from===to)return;
    const [group]=state.meshGroups.splice(from,1);state.meshGroups.splice(to,0,group);
    invalidateMeshSolution();renderMeshGroupEditor();
  }

  function clearMeshGroups() {
    state.meshGroups=[];resetMeshDraft();state.meshSelectedGroupId=null;state.visualSelection=new Set();
    invalidateMeshSolution();renderMeshGroupEditor();draw();
  }

  function meshCoverageError() {
    if(state.meshEditingGroupId)return '正在编辑网片组，请先确认修改或取消草稿';
    if(state.meshDraftSelection.size)return `当前草稿还有 ${state.meshDraftSelection.size} 根钢筋，请先确认网片组`;
    const total=state.model?.bars?.length||0;
    const counts=new Map();
    for(const group of state.meshGroups)for(const index of group.bar_indices)counts.set(index,(counts.get(index)||0)+1);
    const duplicate=[...counts].filter(([,count])=>count>1);
    const missing=(state.model?.bars||[]).filter(bar=>!counts.has(bar.i));
    if(duplicate.length)return `有 ${duplicate.length} 根钢筋被重复分组`;
    if(missing.length)return `仍有 ${missing.length} / ${total} 根钢筋未分组`;
    if(!state.meshGroups.length)return '至少需要一个网片组';
    return null;
  }

  function meshVectorInputs() {
    const raw=[$('meshLongitudinalX').value,$('meshLongitudinalY').value,$('meshLongitudinalZ').value];
    if(raw.every(value=>String(value).trim()===''))return null;
    const values=raw.map(nullableNumber);
    if(values.some(value=>value===null))throw new Error('人工纵轴必须完整填写 X、Y、Z 三个有限数值，或全部留空使用自动识别。');
    if(Math.hypot(...values)<1e-8)throw new Error('人工纵轴向量长度不能为 0。');
    return values;
  }

  function buildMeshPayload() {
    const axis=meshVectorInputs();
    return {mode:'mesh_groups',schema_version:2,model_fingerprint:state.model?.model_fingerprint||'',longitudinal_axis:axis,vertical_axis:[0,0,1],top_elevation_mm:nullableNumber($('meshTopElevation').value),staging_clearance_mm:nullableNumber($('meshDefaultClearance').value)??800,groups:state.meshGroups.map((group,index)=>({group_id:group.group_id,name:group.name,installation_step:index+1,installation_status:group.installation_status,bar_indices:group.bar_indices.slice(),plane_angle_deg:nullableNumber(group.plane_angle_deg),rotation_axis:{transverse_mm:nullableNumber(group.rotation_axis?.transverse_mm),elevation_mm:nullableNumber(group.rotation_axis?.elevation_mm),direction:axis},staging_clearance_mm:nullableNumber(group.staging_clearance_mm)}))};
  }

  function applyResolvedMeshGroups(resolved) {
    state.meshResolved=resolved;
    state.meshDefaults.longitudinalAxis=resolved.longitudinal_axis||state.meshDefaults.longitudinalAxis;
    state.meshDefaults.verticalAxis=resolved.vertical_axis||[0,0,1];
    state.meshDefaults.topElevation=resolved.top_elevation_mm;
    state.meshDefaults.clearance=resolved.staging_clearance_mm??800;
    const axis=state.meshDefaults.longitudinalAxis;
    [$('meshLongitudinalX'),$('meshLongitudinalY'),$('meshLongitudinalZ')].forEach((input,index)=>input.placeholder=`自动 ${Number(axis[index]).toFixed(6)}`);
    $('meshTopElevation').placeholder=`自动 ${Number(resolved.top_elevation_mm).toFixed(3)}`;
    $('meshDefaultClearance').value=resolved.staging_clearance_mm??800;
    const byId=new Map((resolved.groups||[]).map(group=>[group.group_id,group]));
    for(const group of state.meshGroups){
      const solved=byId.get(group.group_id);if(!solved)continue;
      group.resolved_values={plane_angle_deg:solved.plane_angle_deg,axis_transverse_mm:solved.rotation_axis?.transverse_mm,axis_elevation_mm:solved.rotation_axis?.elevation_mm,staging_clearance_mm:solved.staging_clearance_mm};
      group.rotation_axis={...(group.rotation_axis||{}),direction:solved.rotation_axis?.direction};
      group.plane_fit=solved.plane_fit;
    }
    state.groupPaths=new Map((resolved.groups||[]).map(group=>[group.group_id,group]));
    const select=$('meshPreviewGroup');
    select.innerHTML=(resolved.groups||[]).map(group=>`<option value="${escapeHtml(group.group_id)}">${escapeHtml(group.name)} · ${Number(group.plane_angle_deg).toFixed(1)}°</option>`).join('');
    if(state.meshSelectedGroupId&&byId.has(state.meshSelectedGroupId))select.value=state.meshSelectedGroupId;
    else state.meshSelectedGroupId=select.value||null;
    renderMeshGroupEditor();renderMeshPreviewDetails();draw();
  }

  async function solveMeshGroups(openPreview=true) {
    const error=meshCoverageError();if(error){showNote(error,true);return false;}
    const file=$('fileInput').files[0];if(!file){showNote('请选择 IFC 文件。',true);return false;}
    let requestPayload;
    try{requestPayload=buildMeshPayload();}catch(err){showNote(err.message,true);return false;}
    const revision=state.meshRevision;
    const form=new FormData();form.append('file',file);form.append('visual_sequence_json',JSON.stringify(requestPayload));
    $('meshSolveBtn').disabled=true;$('meshRefreshPreviewBtn').disabled=true;showNote('正在识别纵轴、剔除弯头并求解网片平面与旋转轴…');
    try{
      const response=await api('/api/sequence/preview',{method:'POST',body:form});
      if(state.meshRevision!==revision){showNote('求解期间分组或参数已改变，本次旧结果已忽略；请重新求解。',true);return false;}
      applyResolvedMeshGroups(response.mesh_groups||response);
      if(openPreview)setMeshStage('preview');
      showNote('网片参数已求解；请检查主体段、水平初始态和旋转轴预览。');return true;
    }catch(err){showNote(err.message,true);return false;}
    finally{$('meshSolveBtn').disabled=false;$('meshRefreshPreviewBtn').disabled=false;}
  }

  async function saveMeshGroupOrder() {
    const ok=await solveMeshGroups(false);if(!ok)return;
    state.meshGroupPayload=buildMeshPayload();state.visualEditorActive=false;setVisualBoxMode(false);
    $('visualSequenceEditor').classList.add('hidden');$('viewerPlayer').classList.remove('hidden');
    const installed=state.meshGroups.filter(group=>group.installation_status==='preinstalled').length;
    $('meshGroupSummary').textContent=`已保存 ${state.meshGroups.length} 个网片组，其中 ${installed} 组已安装。`;
    showNote('网片组分组、顺序和路径参数已保存，可以开始计算。');draw();
  }

  async function loadMeshGroupPreview() {
    const file=$('fileInput').files[0];if(!file){showNote('请先选择 IFC 文件。',true);return;}
    if(state.visualSourceKey===selectedFileKey()&&state.model?.bars?.length){state.visualMode='groups';showVisualEditor();renderMeshGroupEditor();draw();return;}
    const form=new FormData();form.append('file',file);$('meshGroupBtn').disabled=true;showNote('正在解析 IFC 并生成可分组三维模型…');
    try{
      const model=await api('/api/sequence/preview',{method:'POST',body:form});
      if(state.eventSource){state.eventSource.close();state.eventSource=null;}
      state.currentTask=null;renderTaskList();state.model=model;state.barsByIndex=new Map(model.bars.map(bar=>[bar.i,bar]));
      state.assemblyPaths=new Map();state.groupPaths=new Map();state.robotWaypoints=new Map();state.step=0;state.alpha=0;state.playing=false;
      state.visualMode='groups';state.visualSourceKey=selectedFileKey();state.visualEditorActive=true;state.visualSelection=new Set();state.meshGroups=[];resetMeshDraft();state.meshResolved=null;state.visualBarPayload=null;state.meshGroupPayload=null;state.meshRevision++;
      configureVisualEditorMode();setVisualBoxMode(false);$('visualSequenceEditor').classList.remove('hidden');$('viewerPlayer').classList.add('hidden');$('emptyState').classList.add('hidden');
      $('metricBars').textContent=model.bars.length.toLocaleString('zh-CN');$('metricTypes').textContent=(model.meta?.axis_type_count||0).toLocaleString('zh-CN');$('metricLength').textContent=(model.meta?.axis_total_length_m||0).toFixed(1)+' m';$('metricFeasible').textContent='待划分网片';$('metricFeasible').style.color='var(--orange)';
      computeBounds();fitView();setMeshStage('grouping');renderMeshGroupEditor();$('meshGroupSummary').textContent=`已解析 ${model.bars.length.toLocaleString('zh-CN')} 根钢筋，等待完整分组。`;showNote('模型已加载：点选或开启框选形成第一个网片组。');
    }catch(err){showNote(err.message,true);}finally{updateSequenceGeneratorState();}
  }

  function renderMeshPreviewDetails() {
    const group=(state.meshResolved?.groups||[]).find(item=>item.group_id===state.meshSelectedGroupId)||state.meshResolved?.groups?.[0];
    if(!group){$('meshPreviewDetails').textContent='尚未生成网片路径参数。';return;}
    state.meshSelectedGroupId=group.group_id;
    if([...$('meshPreviewGroup').options].some(option=>option.value===group.group_id))$('meshPreviewGroup').value=group.group_id;
    const fit=group.plane_fit||{},warnings=fit.warnings||[];
    $('meshPreviewStatus').textContent=`${group.name}：水平初始态 → 竖直下降 ${Number(group.staging_clearance_mm).toFixed(0)} mm → 绕纵轴旋转 ${Number(group.plane_angle_deg).toFixed(1)}°`;
    $('meshPreviewDetails').innerHTML=`<strong>${escapeHtml(group.name)}</strong><span>主体段占比 ${(100*Number(fit.main_body_length_ratio||0)).toFixed(1)}%</span><span>拟合可信度 ${(100*Number(fit.confidence||0)).toFixed(1)}%</span><span>拟合残差 ${Number(fit.rms_residual_mm||0).toFixed(2)} mm</span><span>排除弯头段 ${Number(fit.excluded_segment_count||0).toLocaleString('zh-CN')} 段</span><span>旋转轴 T=${Number(group.rotation_axis?.transverse_mm||0).toFixed(1)} mm，Z=${Number(group.rotation_axis?.elevation_mm||0).toFixed(1)} mm</span>${warnings.map(value=>`<em>${escapeHtml(value)}</em>`).join('')}`;
  }

  async function checkHealth() {
    try {
      const data = await api('/api/health');
      $('healthBadge').textContent = `后台在线 · ${data.version}`;
      $('healthBadge').classList.add('ok');
    } catch (err) {
      $('healthBadge').textContent = '后台不可用';
    }
  }

  function taskStatusText(status) {
    return ({queued:'排队', running:'计算中', completed:'已完成', failed:'失败', canceled:'已取消'})[status] || status;
  }

  function collisionDistance(value) {
    return Number(
      value?.maximum_collision_distance_mm ?? value?.max_penetration_mm ??
      value?.maximum_penetration_mm ?? value?.collision_distance_mm ??
      value?.penetration_mm ?? 0
    );
  }

  async function refreshTasks(selectNewest = false) {
    try {
      state.tasks = await api('/api/tasks');
      renderTaskList();
      if (selectNewest && state.tasks[0]) selectTask(state.tasks[0].id);
    } catch (err) {
      console.error(err);
    }
  }

  function renderTaskList() {
    const list = $('taskList');
    list.innerHTML = '';
    if (!state.tasks.length) {
      list.innerHTML = '<div class="upload-note">暂无任务。上传 IFC 后，计算记录会出现在这里。</div>';
      return;
    }
    for (const task of state.tasks) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'task-item' + (state.currentTask?.id === task.id ? ' active' : '');
      button.innerHTML = `<div class="task-name"><span class="status-dot ${task.status}"></span>${escapeHtml(task.filename)}</div>
        <div class="task-meta"><span>${taskStatusText(task.status)} · ${Math.round(task.progress*100)}%</span><span>${task.id.slice(0,8)}</span></div>`;
      button.addEventListener('click', () => selectTask(task.id));
      list.appendChild(button);
    }
  }

  async function selectTask(taskId) {
    try {
      state.visualEditorActive=false;
      setVisualBoxMode(false);
      $('visualSequenceEditor').classList.add('hidden');
      $('viewerPlayer').classList.remove('hidden');
      const task = await api(`/api/tasks/${taskId}`);
      state.currentTask = task;
      renderTaskList();
      renderTask(task);
      subscribeTask(taskId);
      if (task.status === 'completed') await loadTaskResults(taskId);
    } catch (err) {
      showNote(err.message, true);
    }
  }

  function subscribeTask(taskId) {
    if (state.eventSource) state.eventSource.close();
    const source = new EventSource(`/api/tasks/${taskId}/events`);
    state.eventSource = source;
    source.addEventListener('task', async (event) => {
      const task = JSON.parse(event.data);
      state.currentTask = task;
      const idx = state.tasks.findIndex(t => t.id === task.id);
      if (idx >= 0) state.tasks[idx] = task; else state.tasks.unshift(task);
      renderTaskList();
      renderTask(task);
      if(task.status==='running')state.loadedResultTaskId=null;
      if (task.status === 'completed' && state.loadedResultTaskId!==task.id) await loadTaskResults(task.id);
    });
    source.addEventListener('close', () => source.close());
    source.onerror = () => { if (state.currentTask?.status !== 'running') source.close(); };
  }

  function renderTask(task) {
    $('stageLabel').textContent = task.stage || task.status;
    $('progressLabel').textContent = `${Math.round((task.progress || 0)*100)}%`;
    $('progressBar').style.width = `${Math.round((task.progress || 0)*100)}%`;
    $('statusMessage').textContent = task.error || task.message || '';
    $('regenBtn').disabled = task.status !== 'completed' || task.summary?.robot?.supported===false;
    const canRerunMeshGroups=task.sequence_source==='visual_groups'&&['completed','failed','canceled'].includes(task.status);
    $('rerunMeshGroupActions').classList.toggle('hidden',!canRerunMeshGroups);
    $('rerunMeshGroupBtn').disabled=false;
    const s = task.summary;
    $('metricBars').textContent = s?.rebar_count?.toLocaleString('zh-CN') ?? '—';
    $('metricTypes').textContent = s?.type_count?.toLocaleString('zh-CN') ?? '—';
    $('metricLength').textContent = s?.axis_total_length_m != null ? `${s.axis_total_length_m.toFixed(1)} m` : '—';
    const collision = s?.assembly_collision;
    if (collision) {
      const unit=s?.mesh_group_count?'组':'根';
      if(s?.mesh_group_count&&collision.collision_detected_count){
        $('metricFeasible').textContent=`${collision.collision_detected_count} ${unit}碰撞 · ${collision.collision_pair_count||0} 对 · 最大碰撞距离 ${collisionDistance(collision).toFixed(2)} mm`;
      }else{
        $('metricFeasible').textContent = collision.all_paths_collision_free
          ? `${collision.collision_free_count} ${unit}通过`
          : `${collision.collision_detected_count} ${unit}碰撞`;
      }
      if(collision.not_evaluated_group_count)$('metricFeasible').textContent+=`，${collision.not_evaluated_group_count} ${unit}未评估`;
      const installed=collision.preinstalled_group_count??collision.preinstalled_bar_count;
      if (installed) {
        $('metricFeasible').textContent = `${installed} ${unit}已安装，${$('metricFeasible').textContent}`;
      }
      $('metricFeasible').style.color = collision.all_paths_collision_free ? 'var(--ok)' : 'var(--danger)';
    } else if (s?.planner?.strict_graph_feasible === true) {
      $('metricFeasible').textContent = '拓扑可行';
      $('metricFeasible').style.color = 'var(--ok)';
    } else if (s?.planner?.strict_graph_feasible === false) {
      $('metricFeasible').textContent = `循环核 ${s.planner.forced_core_steps}`;
      $('metricFeasible').style.color = 'var(--orange)';
    } else {
      $('metricFeasible').textContent = '—';
    }
    renderDownloads(task);
  }

  function renderDownloads(task) {
    const root = $('downloads'); root.innerHTML = '';
    if (task.status !== 'completed') return;
    const groupMode=Boolean(task.summary?.mesh_group_count);
    const files = groupMode ? [
      ['完整结果包', `/api/tasks/${task.id}/bundle`],
      ['网片组安装顺序', `/api/tasks/${task.id}/files/mesh_group_sequence.csv`],
      ['网片组定义与拟合', `/api/tasks/${task.id}/files/mesh_groups.json`],
      ['网片组六自由度路径', `/api/tasks/${task.id}/files/mesh_group_paths.json`],
      ['网片组碰撞距离明细', `/api/tasks/${task.id}/files/mesh_group_collisions.csv`],
      ['钢筋轴线 JSON', `/api/tasks/${task.id}/files/rebar_axes.json`],
      ['规划摘要', `/api/tasks/${task.id}/files/planning_summary.json`],
      ['后台日志', `/api/tasks/${task.id}/log`],
    ] : [
      ['完整结果包', `/api/tasks/${task.id}/bundle`],
      ['安装顺序 CSV', `/api/tasks/${task.id}/files/installation_sequence.csv`],
      ['六自由度安装路径', `/api/tasks/${task.id}/files/assembly_paths.json`],
      ['碰撞检查报告', `/api/tasks/${task.id}/files/collision_report.json`],
      ['安装路径点 CSV', `/api/tasks/${task.id}/files/assembly_path_waypoints.csv`],
      ['钢筋轴线 JSON', `/api/tasks/${task.id}/files/rebar_axes.json`],
      ['规划摘要', `/api/tasks/${task.id}/files/planning_summary.json`],
      ['TCP 轨迹', `/api/tasks/${task.id}/files/robot/tcp_trajectory.csv`],
      ['ABB RAPID', `/api/tasks/${task.id}/files/robot/rebar_install.mod`],
      ['KUKA KRL', `/api/tasks/${task.id}/files/robot/rebar_install.src`],
      ['URScript', `/api/tasks/${task.id}/files/robot/rebar_install.script`],
      ['后台日志', `/api/tasks/${task.id}/log`],
    ];
    for (const [label, href] of files) {
      const a = document.createElement('a'); a.textContent = label; a.href = href; a.target = '_blank'; root.appendChild(a);
    }
  }

  async function loadTaskResults(taskId) {
    try {
      state.visualEditorActive=false;
      setVisualBoxMode(false);
      $('visualSequenceEditor').classList.add('hidden');
      $('viewerPlayer').classList.remove('hidden');
      $('viewerInfo').textContent = '加载三维数据…';
      const model = await api(`/api/tasks/${taskId}/files/viewer_model.json`);
      if (state.currentTask?.id !== taskId) return;
      state.loadedResultTaskId=taskId;
      state.model = model;
      state.barsByIndex = new Map(model.bars.map(b => [b.i, b]));
      state.assemblyPaths = new Map();
      state.groupPaths = new Map();
      state.robotWaypoints = new Map();
      state.step = 0; state.alpha = 0; state.playing = false;
      $('playBtn').textContent = '播放';
      $('stepSlider').max = String(model.sequence.length);
      $('stepSlider').value = '0';
      computeBounds(); fitView();
      $('emptyState').classList.add('hidden');
      if(model.assembly_unit==='mesh_group'){
        $('robotToggle').checked=false;$('robotToggle').disabled=true;
        try {
          const assembly=await api(`/api/tasks/${taskId}/files/mesh_group_paths.json`);
          state.groupPaths=new Map((assembly.paths||[]).map(path=>[path.group_id,path]));
        } catch (_) { state.groupPaths=new Map((model.group_paths||[]).map(path=>[path.group_id,path])); }
      }else{
        $('robotToggle').disabled=false;
        try {
          const assembly = await api(`/api/tasks/${taskId}/files/assembly_paths.json`);
          state.assemblyPaths = new Map(assembly.paths.map(x => [x.bar_index, x]));
        } catch (_) { state.assemblyPaths = new Map(); }
      }
      try {
        const robot = await api(`/api/tasks/${taskId}/files/robot/robot_waypoints.json`);
        state.robotWaypoints = new Map(robot.map(x => [x.bar_index, x.waypoints]));
      } catch (_) { state.robotWaypoints = new Map(); }
      updateStepUI(); draw();
    } catch (err) {
      $('viewerInfo').textContent = `模型加载失败：${err.message}`;
    }
  }

  function computeBounds() {
    const min = [Infinity,Infinity,Infinity], max = [-Infinity,-Infinity,-Infinity];
    for (const bar of state.model.bars) {
      for (const p of bar.p) for (let k=0;k<3;k++) { if (p[k]<min[k]) min[k]=p[k]; if (p[k]>max[k]) max[k]=p[k]; }
    }
    state.center = min.map((v,k)=>(v+max[k])/2);
    state.span = Math.max(...max.map((v,k)=>v-min[k]), 1);
  }

  function fitView() {
    state.yaw = -0.78; state.pitch = 0.42; state.zoom = 1;
    draw();
  }

  function resizeCanvas() {
    const rect = canvas.getBoundingClientRect();
    const w = Math.max(1, Math.round(rect.width*dpr)), h = Math.max(1, Math.round(rect.height*dpr));
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    state.baseScale = Math.min(w,h)*0.39;
  }

  function rotatePoint(raw, offset=null) {
    let x = (raw[0]-state.center[0]) / state.span;
    let y = (raw[1]-state.center[1]) / state.span;
    let z = (raw[2]-state.center[2]) / state.span;
    if (offset) { x += offset[0]; y += offset[1]; z += offset[2]; }
    const cy=Math.cos(state.yaw), sy=Math.sin(state.yaw), cp=Math.cos(state.pitch), sp=Math.sin(state.pitch);
    const x1=cy*x-sy*y, y1=sy*x+cy*y;
    const y2=cp*y1-sp*z, z2=sp*y1+cp*z;
    const perspective = 1/(2.8-z2*0.75);
    return [canvas.width/2+x1*state.baseScale*state.zoom*perspective*2.8, canvas.height/2-y2*state.baseScale*state.zoom*perspective*2.8, z2];
  }

  function drawPolyline(points, color, width, alpha=1, offset=null, dash=null) {
    if (!points || points.length < 2) return;
    ctx.save();
    if (dash) ctx.setLineDash(dash.map(value=>value*dpr));
    ctx.beginPath();
    const first=rotatePoint(points[0],offset); ctx.moveTo(first[0],first[1]);
    for (let i=1;i<points.length;i++) { const q=rotatePoint(points[i],offset); ctx.lineTo(q[0],q[1]); }
    ctx.strokeStyle=color; ctx.globalAlpha=alpha; ctx.lineWidth=width*dpr; ctx.stroke();
    ctx.restore();
  }


  function quaternionSlerp(a,b,t) {
    let q1=a.slice(),q2=b.slice(),dot=q1.reduce((s,v,i)=>s+v*q2[i],0);
    if(dot<0){q2=q2.map(v=>-v);dot=-dot;}
    if(dot>.9995){const q=q1.map((v,i)=>v+t*(q2[i]-v));const n=Math.hypot(...q)||1;return q.map(v=>v/n);}
    const theta=Math.acos(Math.max(-1,Math.min(1,dot))),sinTheta=Math.sin(theta);
    const u=Math.sin((1-t)*theta)/sinTheta,v=Math.sin(t*theta)/sinTheta;
    return q1.map((x,i)=>u*x+v*q2[i]);
  }

  function rotateByQuaternion(v,q) {
    const [x,y,z,w]=q;
    const uv=[y*v[2]-z*v[1],z*v[0]-x*v[2],x*v[1]-y*v[0]];
    const uuv=[y*uv[2]-z*uv[1],z*uv[0]-x*uv[2],x*uv[1]-y*uv[0]];
    return v.map((value,i)=>value+2*(w*uv[i]+uuv[i]));
  }

  function distance3(a,b) {
    return Math.hypot(...a.map((value,index)=>b[index]-value));
  }

  function quaternionAngleDegrees(a,b) {
    const dot=Math.abs(a.reduce((sum,value,index)=>sum+value*b[index],0));
    return 2*Math.acos(Math.max(-1,Math.min(1,dot)))*180/Math.PI;
  }

  function assemblyPose(path,alpha) {
    const poses=path?.control_poses;
    if(!poses?.length)return null;
    if(poses.length===1){
      return {position:poses[0].position_mm,quaternion:poses[0].quaternion_xyzw,index:0,local:1,segmentCount:0};
    }
    const progress=Math.min(1,Math.max(0,alpha));
    const scaled=progress*(poses.length-1);
    const index=Math.min(poses.length-2,Math.floor(scaled));
    const local=progress>=1?1:scaled-index;
    const a=poses[index],b=poses[index+1];
    return {
      position:a.position_mm.map((value,k)=>value+local*(b.position_mm[k]-value)),
      quaternion:quaternionSlerp(a.quaternion_xyzw,b.quaternion_xyzw,local),
      index,local,segmentCount:poses.length-1,
    };
  }

  function pathMotionInfo(path,alpha) {
    const poses=path?.control_poses,frame=assemblyPose(path,alpha);
    if(!poses?.length||!frame)return null;
    let translationTotal=0,rotationTotal=0,translationDone=0,rotationDone=0;
    for(let i=0;i<poses.length-1;i++){
      const translation=distance3(poses[i].position_mm,poses[i+1].position_mm);
      const rotation=quaternionAngleDegrees(poses[i].quaternion_xyzw,poses[i+1].quaternion_xyzw);
      translationTotal+=translation;rotationTotal+=rotation;
      if(i<frame.index){translationDone+=translation;rotationDone+=rotation;}
      else if(i===frame.index){translationDone+=translation*frame.local;rotationDone+=rotation*frame.local;}
    }
    const currentA=poses[Math.min(frame.index,poses.length-1)];
    const currentB=poses[Math.min(frame.index+1,poses.length-1)];
    const segmentTranslation=distance3(currentA.position_mm,currentB.position_mm);
    const segmentRotation=quaternionAngleDegrees(currentA.quaternion_xyzw,currentB.quaternion_xyzw);
    let phase='就位',phaseClass='done';
    const phaseDefinition=path.phases?.[frame.index];
    if(alpha<.9995&&phaseDefinition?.label){
      phase=phaseDefinition.label;
      phaseClass=segmentRotation>.5?(segmentTranslation>.5?'mixed':'rotation'):'translation';
    }else if(alpha<.9995&&segmentTranslation>.5&&segmentRotation>.5){phase='平移 + 转动';phaseClass='mixed';}
    else if(alpha<.9995&&segmentRotation>.5){phase='转动';phaseClass='rotation';}
    else if(alpha<.9995&&segmentTranslation>.5){phase='平移';phaseClass='translation';}
    if(path.status==='collision_detected'&&(path.collisions||[]).some(collision=>collision.phase===phaseDefinition?.name)){phase+=` · 已记录碰撞`;phaseClass='collision';}
    return {frame,phase,phaseClass,translationTotal,rotationTotal,translationDone,rotationDone,segmentTranslation,segmentRotation};
  }

  function transformPointByPose(point,pivot,position,quaternion) {
    const rotated=rotateByQuaternion(point.map((value,k)=>value-pivot[k]),quaternion);
    return rotated.map((value,k)=>value+position[k]);
  }

  function transformedPointAtAlpha(point,path,alpha) {
    const frame=assemblyPose(path,alpha);if(!frame)return point;
    return transformPointByPose(point,path.pivot_local_mm,frame.position,frame.quaternion);
  }

  function assemblyPoints(bar,path,alpha) {
    const frame=assemblyPose(path,alpha);
    if(!frame)return bar.p;
    const pivot=path.pivot_local_mm;
    return bar.p.map(point=>transformPointByPose(point,pivot,frame.position,frame.quaternion));
  }

  function drawWorldMarker(position,color,radius,label='') {
    const point=rotatePoint(position);
    ctx.save();
    ctx.beginPath();ctx.arc(point[0],point[1],radius*dpr,0,Math.PI*2);
    ctx.fillStyle=color;ctx.shadowColor=color;ctx.shadowBlur=7*dpr;ctx.fill();
    ctx.shadowBlur=0;
    if(label){
      ctx.font=`${10*dpr}px sans-serif`;ctx.textAlign='left';ctx.textBaseline='middle';
      const width=ctx.measureText(label).width+10*dpr;
      ctx.fillStyle='rgba(7,9,11,.82)';ctx.fillRect(point[0]+7*dpr,point[1]-9*dpr,width,18*dpr);
      ctx.fillStyle=color;ctx.fillText(label,point[0]+12*dpr,point[1]);
    }
    ctx.restore();
  }

  function drawPoseAxes(frame,motion) {
    if(!$('poseAxesToggle').checked||!frame)return;
    const origin=frame.position,L=state.span*.055;
    const axes=[[[L,0,0],'#ff6a6a','X'],[[0,L,0],'#62d69a','Y'],[[0,0,L],'#67a9ff','Z']];
    const start=rotatePoint(origin);
    ctx.save();ctx.font=`${10*dpr}px sans-serif`;ctx.textAlign='left';ctx.textBaseline='middle';
    for(const [axis,color,label] of axes){
      const rotated=rotateByQuaternion(axis,frame.quaternion);
      const endWorld=origin.map((value,index)=>value+rotated[index]);
      const end=rotatePoint(endWorld);
      ctx.beginPath();ctx.moveTo(start[0],start[1]);ctx.lineTo(end[0],end[1]);
      ctx.strokeStyle=color;ctx.lineWidth=2*dpr;ctx.stroke();
      ctx.fillStyle=color;ctx.fillText(label,end[0]+3*dpr,end[1]);
    }
    if(motion?.segmentRotation>.5){
      const label=`↻ ${motion.rotationDone.toFixed(1)}°`;
      ctx.font=`${11*dpr}px sans-serif`;ctx.textAlign='center';
      const width=ctx.measureText(label).width+12*dpr;
      ctx.fillStyle='rgba(79,48,105,.88)';ctx.fillRect(start[0]-width/2,start[1]+13*dpr,width,21*dpr);
      ctx.fillStyle='#d8b3ff';ctx.fillText(label,start[0],start[1]+23.5*dpr);
    }
    ctx.restore();
  }

  function drawMotionGuide(bar,path,motion) {
    if(!$('motionGuideToggle').checked||!motion)return;
    const poses=path.control_poses,trajectory=poses.map(p=>p.position_mm);
    const pathColor=path.status==='collision_free'?'#69e6ff':'#ff6767';
    drawPolyline(bar.p,'#60d394',2,.86,null,[7,5]);
    drawPolyline(trajectory,pathColor,1.5,.72,null,[5,5]);
    const completed=trajectory.slice(0,motion.frame.index+1);
    completed.push(motion.frame.position);
    drawPolyline(completed,path.status==='collision_free'?'#ff9d45':'#ff6767',2.4,.95);
    for(const pose of poses)drawWorldMarker(pose.position_mm,pathColor,2.2);
    drawWorldMarker(trajectory[0],pathColor,4,'起点');
    drawWorldMarker(trajectory[trajectory.length-1],'#60d394',4,'安装位置');
    drawWorldMarker(motion.frame.position,path.status==='collision_free'?'#ff9d45':'#ff6767',5,'当前位置');
  }

  function updateMotionHud(path,motion) {
    const visible=Boolean(path&&motion&&state.step<(state.model?.sequence.length||0));
    $('motionHud').classList.toggle('hidden',!visible);
    $('motionLegend').classList.toggle('hidden',!visible||!$('motionGuideToggle').checked);
    if(!visible)return;
    const percent=Math.round(Math.min(1,Math.max(0,state.alpha))*100);
    $('motionPhase').textContent=motion.phase;
    $('motionPhase').className=`motion-phase ${motion.phaseClass}`;
    $('motionPercent').textContent=`${percent}%`;
    $('motionProgressBar').style.width=`${percent}%`;
    $('motionSegment').textContent=motion.frame.segmentCount?`${motion.frame.index+1} / ${motion.frame.segmentCount}`:'就位点';
    $('motionTranslation').textContent=`${Math.round(motion.translationDone)} / ${Math.round(motion.translationTotal)} mm`;
    $('motionRotation').textContent=`${motion.rotationDone.toFixed(1)} / ${motion.rotationTotal.toFixed(1)}°`;
  }

  function drawVisualEditorModel() {
    if(state.visualMode==='groups'){drawMeshEditorModel();return;}
    updateMotionHud(null,null);
    $('motionLegend').classList.add('hidden');
    const orderedSet=new Set(state.visualOrder);
    const available=(state.model?.bars||[]).filter(bar=>!orderedSet.has(bar.i)).sort((a,b)=>barDepth(a)-barDepth(b));
    const ordered=state.visualOrder.map(index=>state.barsByIndex.get(index)).filter(Boolean).sort((a,b)=>barDepth(a)-barDepth(b));
    for(const bar of available)drawPolyline(bar.p,'#8b98a3',.75,.22);
    for(const bar of ordered){
      const installed=state.visualPreinstalled.has(bar.i);
      drawPolyline(bar.p,installed?'#60d394':'#54a7ff',installed?1.8:1.45,.9);
    }
    for(const barIndex of state.visualSelection){
      const selected=state.barsByIndex.get(barIndex);
      if(selected)drawPolyline(selected.p,'#ff9d45',3,1);
    }
    const hint=state.visualBoxMode?'框选模式：拖动矩形选择钢筋':'点选模式：单击钢筋加入';
    $('viewerInfo').textContent='可视化排序：已排 '+state.visualOrder.length.toLocaleString('zh-CN')+
      ' / '+state.model.bars.length.toLocaleString('zh-CN')+' · '+hint;
  }

  function drawRotationAxis(axis,color='#c187ff') {
    if(!axis?.point_mm||!axis?.direction)return;
    const length=state.span*.7,point=axis.point_mm,direction=axis.direction;
    const a=point.map((value,index)=>value-direction[index]*length);
    const b=point.map((value,index)=>value+direction[index]*length);
    drawPolyline([a,b],color,2,.9,null,[8,5]);
    drawWorldMarker(point,color,4,'旋转轴');
  }

  function drawMeshEditorModel() {
    updateMotionHud(null,null);$('motionLegend').classList.add('hidden');
    const assigned=meshAssignedMap();
    const groupsById=new Map(state.meshGroups.map(group=>[group.group_id,group]));
    for(const bar of (state.model?.bars||[]).slice().sort((a,b)=>barDepth(a)-barDepth(b))){
      const group=groupsById.get(assigned.get(bar.i));
      const alpha=group?.group_id===state.meshSelectedGroupId ? .98 : (group ? .78 : .18);
      drawPolyline(bar.p,group?.color||'#89949d',group?1.25:.7,alpha);
    }
    for(const index of state.visualSelection){
      if(state.meshDraftSelection.has(index))continue;
      const bar=state.barsByIndex.get(index);if(bar)drawPolyline(bar.p,'#ffd166',3.1,1);
    }
    if(state.meshStage==='preview'&&state.meshResolved){
      const solved=(state.meshResolved.groups||[]).find(group=>group.group_id===state.meshSelectedGroupId)||state.meshResolved.groups?.[0];
      if(solved){
        const path=solved,alpha=state.meshPreviewAlpha;
        for(const index of solved.bar_indices||[]){
          const bar=state.barsByIndex.get(index);if(!bar)continue;
          drawPolyline(bar.p,'#60d394',1,.28,null,[7,5]);
          const points=assemblyPoints(bar,path,alpha);
          drawPolyline(points,'#ff9d45',2.7,1);
          const ranges=solved.plane_fit?.main_body_segments?.[String(index)]||[];
          for(const [start,end] of ranges)drawPolyline(points.slice(start,end+1),'#69e6ff',3.5,1);
        }
        const motion=pathMotionInfo(path,alpha);drawPoseAxes(motion?.frame,motion);drawRotationAxis(solved.rotation_axis);
      }
    }
    for(const index of state.meshDraftSelection){const bar=state.barsByIndex.get(index);if(bar)drawPolyline(bar.p,'#d8ff3e',3.2,1);}
    const assignedCount=new Set(state.meshGroups.flatMap(group=>group.bar_indices)).size;
    $('viewerInfo').textContent=state.meshStage==='preview'
      ?`网片路径预览 · ${state.meshSelectedGroupId||'未选择'} · ${Math.round(state.meshPreviewAlpha*100)}%`
      :`网片分组：${assignedCount} / ${state.model.bars.length} 根 · ${state.meshGroups.length} 组 · ${state.visualBoxMode?'拖动框选':'单击选择'}`;
  }

  function pointSegmentDistance(px,py,ax,ay,bx,by) {
    const dx=bx-ax,dy=by-ay,lengthSquared=dx*dx+dy*dy;
    if(!lengthSquared)return Math.hypot(px-ax,py-ay);
    const t=Math.max(0,Math.min(1,((px-ax)*dx+(py-ay)*dy)/lengthSquared));
    return Math.hypot(px-(ax+t*dx),py-(ay+t*dy));
  }

  function drawVisualSelectionBox() {
    if(!state.visualBoxMode||!state.visualBoxStart||!state.visualBoxCurrent)return;
    const left=Math.min(state.visualBoxStart[0],state.visualBoxCurrent[0]);
    const top=Math.min(state.visualBoxStart[1],state.visualBoxCurrent[1]);
    const width=Math.abs(state.visualBoxCurrent[0]-state.visualBoxStart[0]);
    const height=Math.abs(state.visualBoxCurrent[1]-state.visualBoxStart[1]);
    ctx.save();
    ctx.fillStyle='rgba(216,255,62,.10)';
    ctx.fillRect(left,top,width,height);
    ctx.strokeStyle='#d8ff3e';ctx.lineWidth=1.5*dpr;
    ctx.setLineDash([6*dpr,4*dpr]);ctx.strokeRect(left,top,width,height);
    ctx.setLineDash([]);ctx.fillStyle='#d8ff3e';ctx.font=(11*dpr)+'px sans-serif';
    ctx.fillText('框选区域',left+6*dpr,top+16*dpr);
    ctx.restore();
  }

  function pointInsideBox(point,box) {
    return point[0]>=box.left&&point[0]<=box.right&&point[1]>=box.top&&point[1]<=box.bottom;
  }

  function segmentIntersectsBox(a,b,box) {
    if(pointInsideBox(a,box)||pointInsideBox(b,box))return true;
    const dx=b[0]-a[0],dy=b[1]-a[1];
    let low=0,high=1;
    const tests=[
      [-dx,a[0]-box.left],[dx,box.right-a[0]],
      [-dy,a[1]-box.top],[dy,box.bottom-a[1]],
    ];
    for(const [p,q] of tests){
      if(Math.abs(p)<1e-12){if(q<0)return false;continue;}
      const ratio=q/p;
      if(p<0){if(ratio>high)return false;if(ratio>low)low=ratio;}
      else{if(ratio<low)return false;if(ratio<high)high=ratio;}
    }
    return true;
  }

  function barIntersectsBox(bar,box) {
    const projected=bar.p.map(point=>rotatePoint(point));
    if(projected.some(point=>pointInsideBox(point,box)))return true;
    for(let index=1;index<projected.length;index++){
      if(segmentIntersectsBox(projected[index-1],projected[index],box))return true;
    }
    return false;
  }

  function selectVisualBarsInBox() {
    if(!state.visualBoxStart||!state.visualBoxCurrent||!state.model)return;
    const box={
      left:Math.min(state.visualBoxStart[0],state.visualBoxCurrent[0]),
      right:Math.max(state.visualBoxStart[0],state.visualBoxCurrent[0]),
      top:Math.min(state.visualBoxStart[1],state.visualBoxCurrent[1]),
      bottom:Math.max(state.visualBoxStart[1],state.visualBoxCurrent[1]),
    };
    const hits=state.model.bars.filter(bar=>barIntersectsBox(bar,box)).sort((a,b)=>a.i-b.i);
    if(state.visualMode==='groups'){
      const assigned=meshAssignedMap(state.meshEditingGroupId);let added=0,blocked=0;
      state.visualSelection=new Set(hits.map(bar=>bar.i));
      for(const bar of hits){if(assigned.has(bar.i)){blocked++;continue;}if(!state.meshDraftSelection.has(bar.i)){state.meshDraftSelection.add(bar.i);added++;}}
      invalidateMeshSolution();renderMeshGroupEditor();draw();
      if(!hits.length)showNote('框选区域内没有检测到钢筋。');
      else showNote(`框选 ${hits.length} 根，加入草稿 ${added} 根${blocked?`；${blocked} 根已属于其他组，未移动`:''}。`,Boolean(blocked));
      return;
    }
    state.visualSelection=new Set(hits.map(bar=>bar.i));
    state.visualSelected=hits.length?hits[hits.length-1].i:null;
    const assigned=new Set(state.visualOrder),added=[];
    for(const bar of hits){
      if(!assigned.has(bar.i)){
        state.visualOrder.push(bar.i);assigned.add(bar.i);added.push(bar.i);
      }
    }
    if(added.length)state.visualBarPayload=null;
    renderVisualSequenceEditor();draw();
    if(!hits.length)showNote('框选区域内没有检测到钢筋。');
    else showNote('框选 '+hits.length+' 根，新增 '+added.length+' 根；多根已按模型索引依次加入。');
  }

  function selectVisualBarAt(event) {
    if(!state.visualEditorActive||!state.model)return;
    const [px,py]=canvasPointFromEvent(event);
    let closest=null,best=12*dpr;
    for(const bar of state.model.bars){
      for(let i=1;i<bar.p.length;i++){
        const a=rotatePoint(bar.p[i-1]),b=rotatePoint(bar.p[i]);
        const distance=pointSegmentDistance(px,py,a[0],a[1],b[0],b[1]);
        if(distance<best){best=distance;closest=bar;}
      }
    }
    if(!closest)return;
    if(state.visualMode==='groups'){
      addMeshDraftBar(closest.i);return;
    }
    if(state.visualOrder.includes(closest.i)){
      state.visualSelected=closest.i;state.visualSelection=new Set([closest.i]);
      renderVisualSequenceEditor();draw();
    }else addVisualBar(closest.i);
  }

  function draw() {
    resizeCanvas();
    ctx.clearRect(0,0,canvas.width,canvas.height);
    if (!state.model) { updateMotionHud(null,null); return; }
    if(state.visualEditorActive){drawVisualEditorModel();drawAxes();drawVisualSelectionBox();return;}
    const sequence=state.model.sequence;
    if ($('ghostToggle').checked) {
      for (const bar of state.model.bars) drawPolyline(bar.p, '#80909c', .55, .12);
    }
    if(state.model.assembly_unit==='mesh_group'){
      drawMeshGroupTask();drawAxes();return;
    }
    // Installed bars.
    const installed=[], installedIds=new Set();
    for (const barIndex of (state.model.initial_installed || [])) {
      const bar=state.barsByIndex.get(barIndex);
      if (bar && !installedIds.has(barIndex)) { installed.push(bar); installedIds.add(barIndex); }
    }
    for (let s=0;s<Math.min(state.step,sequence.length);s++) {
      const barIndex=sequence[s].i, bar=state.barsByIndex.get(barIndex);
      if (bar && !installedIds.has(barIndex)) { installed.push(bar); installedIds.add(barIndex); }
    }
    installed.sort((a,b)=>barDepth(a)-barDepth(b));
    for (const bar of installed) drawPolyline(bar.p, '#54a7ff', 1.15, .82);

    if (state.step < sequence.length) {
      const current=sequence[state.step], bar=state.barsByIndex.get(current.i);
      if (bar) {
        const path=state.assemblyPaths.get(current.i);
        if(path?.control_poses?.length){
          const motion=pathMotionInfo(path,state.alpha);
          drawMotionGuide(bar,path,motion);
          const points=assemblyPoints(bar,path,state.alpha);
          const color=path.status==='collision_free'?'#ff9d45':'#ff6767';
          drawPolyline(points,color,2.6,1);
          drawPoseAxes(motion?.frame,motion);
          updateMotionHud(path,motion);
        }else{
          const travel=1.25*(1-state.alpha);
          const offset=current.d.map(x=>-x*travel);
          drawPolyline(bar.p,'#ff9d45',2.6,1,offset);
          updateMotionHud(null,null);
        }
        if ($('robotToggle').checked) drawRobotPath(current.i);
      }
    } else updateMotionHud(null,null);
    drawAxes();
  }

  function drawMeshGroupTask() {
    const sequence=state.model.sequence||[],installedIds=new Set(state.model.initial_installed||[]);
    const collidedInstalledIds=new Set();
    for(let step=0;step<Math.min(state.step,sequence.length);step++){
      const previous=sequence[step],previousPath=state.groupPaths.get(previous.group_id)||previous;
      for(const index of previous.bar_indices||[]){installedIds.add(index);if(previousPath.status==='collision_detected')collidedInstalledIds.add(index);}
    }
    const installed=[...installedIds].map(index=>state.barsByIndex.get(index)).filter(Boolean).sort((a,b)=>barDepth(a)-barDepth(b));
    for(const bar of installed)drawPolyline(bar.p,collidedInstalledIds.has(bar.i)?'#d97878':'#54a7ff',1.2,.84);
    if(state.step>=sequence.length){updateMotionHud(null,null);return;}
    const current=sequence[state.step],path=state.groupPaths.get(current.group_id)||current;
    if(!path?.control_poses?.length){updateMotionHud(null,null);return;}
    const motion=pathMotionInfo(path,state.alpha),color=path.status==='collision_detected'?'#ff6767':'#ff9d45';
    if($('motionGuideToggle').checked){
      const centroid=current.plane_fit?.centroid_mm;
      const trajectory=centroid
        ?Array.from({length:41},(_,index)=>transformedPointAtAlpha(centroid,path,index/40))
        :path.control_poses.map(pose=>pose.position_mm);
      drawPolyline(trajectory,path.status==='collision_detected'?'#ff6767':'#69e6ff',1.8,.9,null,[6,4]);
      for(const fraction of [0,.5,1])drawWorldMarker(centroid?transformedPointAtAlpha(centroid,path,fraction):path.control_poses[Math.round(fraction*(path.control_poses.length-1))].position_mm,'#69e6ff',2.4);
      drawRotationAxis(path.rotation_axis);
    }
    for(const index of current.bar_indices||path.bar_indices||[]){
      const bar=state.barsByIndex.get(index);if(!bar)continue;
      if($('motionGuideToggle').checked)drawPolyline(bar.p,'#60d394',1,.38,null,[7,5]);
      drawPolyline(assemblyPoints(bar,path,state.alpha),color,2.4,1);
    }
    const collisionPose=(path.worst_collision||path.first_collision)?.collision_pose;
    if(collisionPose&&$('motionGuideToggle').checked){
      for(const index of current.bar_indices||path.bar_indices||[]){
        const bar=state.barsByIndex.get(index);if(!bar)continue;
        const points=bar.p.map(point=>transformPointByPose(point,path.pivot_local_mm,collisionPose.position_mm,collisionPose.quaternion_xyzw));
        drawPolyline(points,'#ff6767',1.7,.42,null,[5,4]);
      }
    }
    const worstCollision=path.worst_collision||path.first_collision;
    if(worstCollision?.collision_position_mm)drawWorldMarker(worstCollision.collision_position_mm,'#ff6767',6,`碰撞距离 ${collisionDistance(worstCollision).toFixed(2)} mm`);
    drawPoseAxes(motion?.frame,motion);updateMotionHud(path,motion);
  }

  function barDepth(bar) {
    const p=bar.p[Math.floor(bar.p.length/2)]; return rotatePoint(p)[2];
  }

  function drawRobotPath(barIndex) {
    const waypoints=state.robotWaypoints.get(barIndex); if (!waypoints?.length) return;
    const points=waypoints.map(w=>w.position_mm);
    for (let i=0;i<points.length-1;i++) drawPolyline([points[i],points[i+1]], '#d8ff3e', 1.35, .9);
    for (const p of points) { const q=rotatePoint(p); ctx.beginPath();ctx.arc(q[0],q[1],3.2*dpr,0,Math.PI*2);ctx.fillStyle='#d8ff3e';ctx.fill(); }
  }

  function drawAxes() {
    const origin=state.center, L=state.span*.07;
    const axes=[[[origin[0]+L,origin[1],origin[2]],'#ff6a6a','X'],[[origin[0],origin[1]+L,origin[2]],'#62d69a','Y'],[[origin[0],origin[1],origin[2]+L],'#67a9ff','Z']];
    const o=rotatePoint(origin);
    ctx.font=`${11*dpr}px sans-serif`;ctx.textAlign='left';
    for(const [end,color,label] of axes){const q=rotatePoint(end);ctx.beginPath();ctx.moveTo(o[0],o[1]);ctx.lineTo(q[0],q[1]);ctx.strokeStyle=color;ctx.lineWidth=1.5*dpr;ctx.stroke();ctx.fillStyle=color;ctx.fillText(label,q[0]+3*dpr,q[1]);}
  }

  function updateStepUI() {
    const total=state.model?.sequence.length || 0;
    const initialInstalled=state.model?.initial_installed?.length || 0;
    $('stepSlider').value=String(state.step);
    $('stepText').textContent=`${state.step.toLocaleString('zh-CN')} / ${total.toLocaleString('zh-CN')}`;
    if (state.model && state.step<total) {
      if(state.model.assembly_unit==='mesh_group'){
        const item=state.model.sequence[state.step],path=state.groupPaths.get(item.group_id)||item;
        const status=path?.status==='collision_detected'
          ?`记录 ${path.collision_pair_count||0} 对碰撞 · 最大碰撞距离 ${collisionDistance(path).toFixed(2)} mm，继续播放`
          :'无碰撞';
        $('viewerInfo').textContent=`当前安装：第 ${state.step+1} 组 · ${item.name||item.group_id} · ${(item.bar_indices||[]).length} 根 · ${status}`;
        return;
      }
      const item=state.model.sequence[state.step],path=state.assemblyPaths.get(item.i);
      const bar=state.barsByIndex.get(item.i);
      const barLabel=bar?.n?`钢筋 ${bar.n}`:`钢筋索引 ${item.i}`;
      const pathText=path?` · ${path.path_type} · ${path.status==='collision_free'?'无碰撞':'检测到碰撞'}`:'';
      $('viewerInfo').textContent=`当前安装：第 ${state.step+1} 根 · ${barLabel}${pathText}`;
    } else if (total) {
      const collisionPaths=state.model?.assembly_unit==='mesh_group'?state.model.sequence.map(item=>state.groupPaths.get(item.group_id)||item).filter(path=>path.status==='collision_detected'):[];
      const pairCount=collisionPaths.reduce((sum,path)=>sum+Number(path.collision_pair_count||0),0);
      const maximum=Math.max(0,...collisionPaths.map(collisionDistance));
      $('viewerInfo').textContent=collisionPaths.length?`模拟完成 · ${collisionPaths.length} 组、${pairCount} 对碰撞 · 最大碰撞距离 ${maximum.toFixed(2)} mm`:`安装完成 · ${total.toLocaleString('zh-CN')} ${state.model?.assembly_unit==='mesh_group'?'组':'根'}`;
    }
    else if (initialInstalled) $('viewerInfo').textContent=`模型中的 ${initialInstalled.toLocaleString('zh-CN')} 根钢筋均标记为已安装`;
  }

  function animate(now) {
    const dt=Math.min(.08,(now-state.lastFrame)/1000);state.lastFrame=now;
    if(state.playing&&state.model&&!state.visualEditorActive){
      state.alpha+=dt*state.speed*2.2;
      if(state.alpha>=1){state.alpha=0;state.step++;
        if(state.step>=state.model.sequence.length){state.step=state.model.sequence.length;state.playing=false;$('playBtn').textContent='播放';}
        updateStepUI();
      }
      draw();
    }
    requestAnimationFrame(animate);
  }

  async function submitTask() {
    const file=$('fileInput').files[0];
    if(!file){showNote('请选择 IFC 文件。',true);return;}
    const sequenceSource=$('sequenceSource').value,sequenceFile=$('sequenceFile').files[0];
    let sequencePayload=null;
    if(sequenceSource==='excel'&&!sequenceFile){showNote('请选择 Excel 安装顺序表。',true);return;}
    if(sequenceSource==='visual'){
      sequencePayload=state.visualBarPayload;
      if(!Array.isArray(sequencePayload?.items)||!sequencePayload.items.length||state.visualSourceKey!==selectedFileKey()){
        showNote('请先打开可视化排序，完成全部钢筋顺序并保存。',true);return;
      }
    }
    if(sequenceSource==='visual_groups'){
      sequencePayload=state.meshGroupPayload;
      if(sequencePayload?.mode!=='mesh_groups'||sequencePayload?.schema_version!==2||!Array.isArray(sequencePayload?.groups)||!sequencePayload.groups.length||state.visualSourceKey!==selectedFileKey()){
        showNote('请先完成全部钢筋的网片分组、安装顺序和路径参数并保存。',true);return;
      }
    }
    const options={
      clearance_mm:Number($('clearance').value), axis_simplify_mm:Number($('simplify').value), candidate_axes:['z','y','x'],
      sequence_source:sequenceSource, generate_assembly_paths:$('assemblyEnabled').checked,
      assembly_translation_step_mm:Number($('collisionTranslation').value),
      assembly_rotation_step_deg:Number($('collisionRotation').value), assembly_rrt_iterations:350, assembly_random_seed:17,
      generate_robot_path:sequenceSource==='visual_groups'?false:$('robotEnabled').checked,
      robot_linear_speed_mm_s:Number($('linearSpeed').value), robot_angular_speed_deg_s:45,
      robot_sample_period_s:Number($('samplePeriod').value), outside_margin_mm:800, preinsert_distance_mm:250, retreat_distance_mm:300, grasp_fraction:.5,
    };
    const form=new FormData();form.append('file',file);
    if(sequenceSource==='excel')form.append('sequence_file',sequenceFile);
    if(sequencePayload)form.append('visual_sequence_json',JSON.stringify(sequencePayload));
    form.append('options_json',JSON.stringify(options));
    $('submitBtn').disabled=true;showNote('正在上传模型…');
    try{
      const result=await api('/api/tasks',{method:'POST',body:form});
      showNote(`任务已创建：${result.task_id.slice(0,8)}`);await refreshTasks();await selectTask(result.task_id);
    }catch(err){showNote(err.message,true);}finally{$('submitBtn').disabled=false;}
  }

  async function regenerateRobot() {
    if(!state.currentTask)return;
    const body={linear_speed_mm_s:Number($('regenLinear').value),angular_speed_deg_s:Number($('regenAngular').value),sample_period_s:Number($('samplePeriod').value),outside_margin_mm:Number($('regenMargin').value),preinsert_distance_mm:Number($('regenPre').value),retreat_distance_mm:300,grasp_fraction:.5};
    try{await api(`/api/tasks/${state.currentTask.id}/robot`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});state.robotWaypoints=new Map();subscribeTask(state.currentTask.id);}catch(err){alert(err.message);}
  }

  async function rerunMeshGroupTask() {
    if(!state.currentTask)return;
    const sourceTask=state.currentTask;
    const button=$('rerunMeshGroupBtn');
    button.disabled=true;
    showNote('正在复用原 IFC、网片分组和安装顺序创建新任务…');
    try{
      const result=await api(`/api/tasks/${sourceTask.id}/rerun`,{method:'POST'});
      showNote(`已创建重新计算任务：${result.task_id.slice(0,8)}，原任务保持不变。`);
      await refreshTasks();
      await selectTask(result.task_id);
    }catch(err){showNote(err.message,true);button.disabled=false;}
  }

  function showNote(message,error=false){$('uploadNote').textContent=message;$('uploadNote').style.color=error?'var(--danger)':'var(--muted)';}
  function escapeHtml(value){return String(value).replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));}

  // UI events.
  $('sequenceSource').addEventListener('change',e=>{
    $('sequenceUpload').classList.toggle('hidden',e.target.value!=='excel');
    $('visualSequenceSetup').classList.toggle('hidden',e.target.value!=='visual');
    $('meshGroupSetup').classList.toggle('hidden',e.target.value!=='visual_groups');
    $('assemblyEnabled').disabled=e.target.value==='visual_groups';
    $('robotEnabled').disabled=e.target.value==='visual_groups';
    if(e.target.value==='visual_groups'){$('assemblyEnabled').checked=true;$('robotEnabled').checked=false;}
  });
  $('sequenceFile').addEventListener('change',e=>{
    $('sequenceFileLabel').textContent=e.target.files[0]?.name||'选择安装顺序表';
  });
  $('generateSequenceBtn').addEventListener('click',generateSequenceWorkbook);
  $('visualSequenceBtn').addEventListener('click',loadVisualSequencePreview);
  $('meshGroupBtn').addEventListener('click',loadMeshGroupPreview);
  $('visualBoxSelectBtn').addEventListener('click',()=>setVisualBoxMode(!state.visualBoxMode));
  $('visualFillBtn').addEventListener('click',fillVisualOrder);
  $('visualClearBtn').addEventListener('click',()=>state.visualMode==='groups'?clearMeshGroups():clearVisualOrder());
  $('visualSaveBtn').addEventListener('click',()=>state.visualMode==='groups'?saveMeshGroupOrder():saveVisualOrder());
  $('visualCloseBtn').addEventListener('click',closeVisualEditor);
  $('visualSequenceSearch').addEventListener('input',renderVisualSequenceEditor);
  $('visualAvailableList').addEventListener('click',event=>{
    const button=event.target.closest('[data-add]');
    if(button)addVisualBar(Number(button.dataset.add));
  });
  $('visualOrderedList').addEventListener('click',event=>{
    const row=event.target.closest('[data-order-index]');
    if(!row)return;
    const from=Number(row.dataset.orderIndex),barIndex=Number(row.dataset.barIndex);
    const moveButton=event.target.closest('[data-move]');
    const removeButton=event.target.closest('[data-remove]');
    if(moveButton){moveVisualBar(from,from+Number(moveButton.dataset.move));return;}
    if(removeButton){
      state.visualOrder.splice(from,1);state.visualPreinstalled.delete(barIndex);
      state.visualSelection.delete(barIndex);
      state.visualSelected=null;state.visualBarPayload=null;renderVisualSequenceEditor();draw();return;
    }
    if(!event.target.matches('input')){
      state.visualSelected=barIndex;state.visualSelection=new Set([barIndex]);
      renderVisualSequenceEditor();draw();
    }
  });
  $('visualOrderedList').addEventListener('change',event=>{
    if(!event.target.matches('[data-installed]'))return;
    const barIndex=Number(event.target.dataset.installed);
    if(event.target.checked)state.visualPreinstalled.add(barIndex);else state.visualPreinstalled.delete(barIndex);
    state.visualBarPayload=null;renderVisualSequenceEditor();draw();
  });
  $('visualOrderedList').addEventListener('dragstart',event=>{
    const row=event.target.closest('[data-order-index]');
    state.visualDragIndex=row?Number(row.dataset.orderIndex):null;
    if(event.dataTransfer)event.dataTransfer.effectAllowed='move';
  });
  $('visualOrderedList').addEventListener('dragover',event=>{event.preventDefault();});
  $('visualOrderedList').addEventListener('drop',event=>{
    event.preventDefault();
    const row=event.target.closest('[data-order-index]');
    if(row&&state.visualDragIndex!==null)moveVisualBar(state.visualDragIndex,Number(row.dataset.orderIndex));
    state.visualDragIndex=null;
  });
  document.querySelectorAll('[data-mesh-stage]').forEach(button=>button.addEventListener('click',()=>setMeshStage(button.dataset.meshStage)));
  $('meshGroupSearch').addEventListener('input',renderMeshGroupEditor);
  $('meshUnassignedList').addEventListener('click',event=>{
    const button=event.target.closest('[data-mesh-add]');if(button)addMeshDraftBar(Number(button.dataset.meshAdd));
  });
  $('meshDraftList').addEventListener('click',event=>{
    const button=event.target.closest('[data-mesh-draft-remove]');if(button)addMeshDraftBar(Number(button.dataset.meshDraftRemove));
  });
  $('meshConfirmGroupBtn').addEventListener('click',confirmMeshGroup);
  $('meshCancelDraftBtn').addEventListener('click',()=>{resetMeshDraft();state.visualSelection=new Set();renderMeshGroupEditor();draw();});
  $('meshReassignBtn').addEventListener('click',reassignSelectedMeshBars);
  $('meshGroupList').addEventListener('click',event=>{
    const edit=event.target.closest('[data-mesh-edit]'),remove=event.target.closest('[data-mesh-delete]'),card=event.target.closest('[data-mesh-group]');
    if(edit){editMeshGroup(edit.dataset.meshEdit);return;}if(remove){deleteMeshGroup(remove.dataset.meshDelete);return;}
    if(card){state.meshSelectedGroupId=card.dataset.meshGroup;const group=meshGroupById(state.meshSelectedGroupId);state.visualSelection=new Set(group?.bar_indices||[]);renderMeshGroupEditor();draw();}
  });
  $('meshParameterList').addEventListener('input',event=>{
    const card=event.target.closest('[data-mesh-group-id]');if(!card)return;
    const group=meshGroupById(card.dataset.meshGroupId),field=event.target.dataset.meshField;if(!group||!field)return;
    if(field==='installation_status')group.installation_status=event.target.checked?'preinstalled':'pending';
    else if(field==='plane_angle_deg')group.plane_angle_deg=nullableNumber(event.target.value);
    else if(field==='axis_transverse')group.rotation_axis.transverse_mm=nullableNumber(event.target.value);
    else if(field==='axis_elevation')group.rotation_axis.elevation_mm=nullableNumber(event.target.value);
    else if(field==='staging_clearance_mm')group.staging_clearance_mm=nullableNumber(event.target.value);
    invalidateMeshSolution();
  });
  $('meshParameterList').addEventListener('click',event=>{
    const button=event.target.closest('[data-mesh-move]'),card=event.target.closest('[data-mesh-order-index]');
    if(button&&card)moveMeshGroup(Number(card.dataset.meshOrderIndex),Number(card.dataset.meshOrderIndex)+Number(button.dataset.meshMove));
  });
  $('meshParameterList').addEventListener('dragstart',event=>{
    const card=event.target.closest('[data-mesh-order-index]');state.meshDragIndex=card?Number(card.dataset.meshOrderIndex):null;
  });
  $('meshParameterList').addEventListener('dragover',event=>event.preventDefault());
  $('meshParameterList').addEventListener('drop',event=>{
    event.preventDefault();const card=event.target.closest('[data-mesh-order-index]');
    if(card&&state.meshDragIndex!==null)moveMeshGroup(state.meshDragIndex,Number(card.dataset.meshOrderIndex));state.meshDragIndex=null;
  });
  for(const id of ['meshLongitudinalX','meshLongitudinalY','meshLongitudinalZ','meshTopElevation','meshDefaultClearance'])$(id).addEventListener('input',invalidateMeshSolution);
  $('meshSolveBtn').addEventListener('click',()=>solveMeshGroups(true));
  $('meshRefreshPreviewBtn').addEventListener('click',()=>solveMeshGroups(true));
  $('meshPreviewGroup').addEventListener('change',event=>{state.meshSelectedGroupId=event.target.value;renderMeshPreviewDetails();draw();});
  $('meshPreviewSlider').addEventListener('input',event=>{state.meshPreviewAlpha=Number(event.target.value)/100;$('meshPreviewPercent').textContent=`${event.target.value}%`;draw();});
  $('submitBtn').addEventListener('click',submitTask);
  $('regenBtn').addEventListener('click',regenerateRobot);
  $('rerunMeshGroupBtn').addEventListener('click',rerunMeshGroupTask);
  $('fitBtn').addEventListener('click',fitView);
  $('playBtn').addEventListener('click',()=>{if(!state.model)return;if(state.step>=state.model.sequence.length)state.step=0;state.playing=!state.playing;$('playBtn').textContent=state.playing?'暂停':'播放';updateStepUI();});
  $('prevBtn').addEventListener('click',()=>{state.playing=false;$('playBtn').textContent='播放';state.step=Math.max(0,state.step-1);state.alpha=0;updateStepUI();draw();});
  $('nextBtn').addEventListener('click',()=>{if(!state.model)return;state.playing=false;$('playBtn').textContent='播放';state.step=Math.min(state.model.sequence.length,state.step+1);state.alpha=0;updateStepUI();draw();});
  $('stepSlider').addEventListener('input',e=>{state.playing=false;$('playBtn').textContent='播放';state.step=Number(e.target.value);state.alpha=0;updateStepUI();draw();});
  $('speedSelect').addEventListener('change',e=>state.speed=Number(e.target.value));
  $('ghostToggle').addEventListener('change',draw);
  $('motionGuideToggle').addEventListener('change',draw);
  $('poseAxesToggle').addEventListener('change',draw);
  $('robotToggle').addEventListener('change',draw);
  canvas.addEventListener('pointerdown',e=>{
    state.dragging=true;state.lastX=e.clientX;state.lastY=e.clientY;
    state.dragStartX=e.clientX;state.dragStartY=e.clientY;canvas.setPointerCapture(e.pointerId);
    if(state.visualEditorActive&&state.visualBoxMode){
      state.visualBoxStart=canvasPointFromEvent(e);
      state.visualBoxCurrent=state.visualBoxStart.slice();
      draw();
    }
  });
  canvas.addEventListener('pointermove',e=>{
    if(!state.dragging)return;
    if(state.visualEditorActive&&state.visualBoxMode){
      state.visualBoxCurrent=canvasPointFromEvent(e);draw();return;
    }
    state.yaw+=(e.clientX-state.lastX)*.006;
    state.pitch=Math.max(-1.45,Math.min(1.45,state.pitch+(e.clientY-state.lastY)*.006));
    state.lastX=e.clientX;state.lastY=e.clientY;draw();
  });
  canvas.addEventListener('pointerup',e=>{
    const moved=Math.hypot(e.clientX-state.dragStartX,e.clientY-state.dragStartY);
    state.dragging=false;
    if(state.visualEditorActive&&state.visualBoxMode){
      state.visualBoxCurrent=canvasPointFromEvent(e);
      if(moved<5)selectVisualBarAt(e);else selectVisualBarsInBox();
      state.visualBoxStart=null;state.visualBoxCurrent=null;draw();return;
    }
    if(moved<5)selectVisualBarAt(e);
  });
  canvas.addEventListener('pointercancel',()=>{
    state.dragging=false;state.visualBoxStart=null;state.visualBoxCurrent=null;draw();
  });
  canvas.addEventListener('wheel',e=>{e.preventDefault();state.zoom=Math.max(.2,Math.min(8,state.zoom*Math.exp(-e.deltaY*.001)));draw();},{passive:false});
  canvas.addEventListener('dblclick',()=>{if(!state.visualBoxMode)fitView();});
  window.addEventListener('resize',draw);
  const dropzone=$('dropzone');
  for(const name of ['dragenter','dragover'])dropzone.addEventListener(name,e=>{e.preventDefault();dropzone.classList.add('drag');});
  for(const name of ['dragleave','drop'])dropzone.addEventListener(name,e=>{e.preventDefault();dropzone.classList.remove('drag');});
  dropzone.addEventListener('drop',e=>{const files=e.dataTransfer.files;if(files.length){resetVisualSequence();$('fileInput').files=files;$('fileLabel').textContent=files[0].name;updateSequenceGeneratorState();}});
  $('fileInput').addEventListener('change',e=>{resetVisualSequence();$('fileLabel').textContent=e.target.files[0]?.name||'拖入 IFC 文件或点击选择';updateSequenceGeneratorState();});

  updateSequenceGeneratorState();checkHealth();refreshTasks();requestAnimationFrame(animate);
})();

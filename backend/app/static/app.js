(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const state = {
    tasks: [], currentTask: null, eventSource: null,
    model: null, barsByIndex: new Map(), robotWaypoints: new Map(), assemblyPaths: new Map(),
    step: 0, alpha: 0, playing: false, speed: 1,
    lastFrame: performance.now(), yaw: -0.78, pitch: 0.42, zoom: 1,
    dragging: false, lastX: 0, lastY: 0,
    center: [0,0,0], span: 1, baseScale: 1,
  };

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
      if (task.status === 'completed' && !state.model) await loadTaskResults(task.id);
    });
    source.addEventListener('close', () => source.close());
    source.onerror = () => { if (state.currentTask?.status !== 'running') source.close(); };
  }

  function renderTask(task) {
    $('stageLabel').textContent = task.stage || task.status;
    $('progressLabel').textContent = `${Math.round((task.progress || 0)*100)}%`;
    $('progressBar').style.width = `${Math.round((task.progress || 0)*100)}%`;
    $('statusMessage').textContent = task.error || task.message || '';
    $('regenBtn').disabled = task.status !== 'completed';
    const s = task.summary;
    $('metricBars').textContent = s?.rebar_count?.toLocaleString('zh-CN') ?? '—';
    $('metricTypes').textContent = s?.type_count?.toLocaleString('zh-CN') ?? '—';
    $('metricLength').textContent = s?.axis_total_length_m != null ? `${s.axis_total_length_m.toFixed(1)} m` : '—';
    const collision = s?.assembly_collision;
    if (collision) {
      $('metricFeasible').textContent = collision.all_paths_collision_free
        ? `${collision.collision_free_count} 根通过`
        : `${collision.collision_detected_count} 根碰撞`;
      if (collision.preinstalled_bar_count) {
        $('metricFeasible').textContent = `${collision.preinstalled_bar_count} 根已安装，${$('metricFeasible').textContent}`;
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
    const files = [
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
      $('viewerInfo').textContent = '加载三维数据…';
      const model = await api(`/api/tasks/${taskId}/files/viewer_model.json`);
      if (state.currentTask?.id !== taskId) return;
      state.model = model;
      state.barsByIndex = new Map(model.bars.map(b => [b.i, b]));
      state.assemblyPaths = new Map();
      state.robotWaypoints = new Map();
      state.step = 0; state.alpha = 0; state.playing = false;
      $('playBtn').textContent = '播放';
      $('stepSlider').max = String(model.sequence.length);
      $('stepSlider').value = '0';
      computeBounds(); fitView();
      $('emptyState').classList.add('hidden');
      try {
        const assembly = await api(`/api/tasks/${taskId}/files/assembly_paths.json`);
        state.assemblyPaths = new Map(assembly.paths.map(x => [x.bar_index, x]));
      } catch (_) { state.assemblyPaths = new Map(); }
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
    if(path.status==='collision_detected'){phase='碰撞路径';phaseClass='collision';}
    else if(alpha<.9995&&segmentTranslation>.5&&segmentRotation>.5){phase='平移 + 转动';phaseClass='mixed';}
    else if(alpha<.9995&&segmentRotation>.5){phase='转动';phaseClass='rotation';}
    else if(alpha<.9995&&segmentTranslation>.5){phase='平移';phaseClass='translation';}
    return {frame,phase,phaseClass,translationTotal,rotationTotal,translationDone,rotationDone,segmentTranslation,segmentRotation};
  }

  function assemblyPoints(bar,path,alpha) {
    const frame=assemblyPose(path,alpha);
    if(!frame)return bar.p;
    const pivot=path.pivot_local_mm;
    return bar.p.map(point=>{
      const rotated=rotateByQuaternion(point.map((value,k)=>value-pivot[k]),frame.quaternion);
      return rotated.map((value,k)=>value+frame.position[k]);
    });
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

  function draw() {
    resizeCanvas();
    ctx.clearRect(0,0,canvas.width,canvas.height);
    if (!state.model) { updateMotionHud(null,null); return; }
    const sequence=state.model.sequence;
    if ($('ghostToggle').checked) {
      for (const bar of state.model.bars) drawPolyline(bar.p, '#80909c', .55, .12);
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
      const item=state.model.sequence[state.step],path=state.assemblyPaths.get(item.i);
      const bar=state.barsByIndex.get(item.i);
      const barLabel=bar?.n?`钢筋 ${bar.n}`:`钢筋索引 ${item.i}`;
      const pathText=path?` · ${path.path_type} · ${path.status==='collision_free'?'无碰撞':'检测到碰撞'}`:'';
      $('viewerInfo').textContent=`当前安装：第 ${state.step+1} 根 · ${barLabel}${pathText}`;
    } else if (total) $('viewerInfo').textContent=`安装完成 · ${total.toLocaleString('zh-CN')} 根`;
    else if (initialInstalled) $('viewerInfo').textContent=`模型中的 ${initialInstalled.toLocaleString('zh-CN')} 根钢筋均标记为已安装`;
  }

  function animate(now) {
    const dt=Math.min(.08,(now-state.lastFrame)/1000);state.lastFrame=now;
    if(state.playing&&state.model){
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
    if(sequenceSource==='excel'&&!sequenceFile){showNote('请选择 Excel 安装顺序表。',true);return;}
    const options={
      clearance_mm:Number($('clearance').value), axis_simplify_mm:Number($('simplify').value), candidate_axes:['z','y','x'],
      sequence_source:sequenceSource, generate_assembly_paths:$('assemblyEnabled').checked,
      assembly_translation_step_mm:Number($('collisionTranslation').value),
      assembly_rotation_step_deg:Number($('collisionRotation').value), assembly_rrt_iterations:350, assembly_random_seed:17,
      generate_robot_path:$('robotEnabled').checked,
      robot_linear_speed_mm_s:Number($('linearSpeed').value), robot_angular_speed_deg_s:45,
      robot_sample_period_s:Number($('samplePeriod').value), outside_margin_mm:800, preinsert_distance_mm:250, retreat_distance_mm:300, grasp_fraction:.5,
    };
    const form=new FormData();form.append('file',file);
    if(sequenceSource==='excel')form.append('sequence_file',sequenceFile);
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

  function showNote(message,error=false){$('uploadNote').textContent=message;$('uploadNote').style.color=error?'var(--danger)':'var(--muted)';}
  function escapeHtml(value){return String(value).replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));}

  // UI events.
  $('sequenceSource').addEventListener('change',e=>{
    $('sequenceUpload').classList.toggle('hidden',e.target.value!=='excel');
  });
  $('sequenceFile').addEventListener('change',e=>{
    $('sequenceFileLabel').textContent=e.target.files[0]?.name||'选择安装顺序表';
  });
  $('generateSequenceBtn').addEventListener('click',generateSequenceWorkbook);
  $('submitBtn').addEventListener('click',submitTask);
  $('regenBtn').addEventListener('click',regenerateRobot);
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
  canvas.addEventListener('pointerdown',e=>{state.dragging=true;state.lastX=e.clientX;state.lastY=e.clientY;canvas.setPointerCapture(e.pointerId);});
  canvas.addEventListener('pointermove',e=>{if(!state.dragging)return;state.yaw+=(e.clientX-state.lastX)*.006;state.pitch=Math.max(-1.45,Math.min(1.45,state.pitch+(e.clientY-state.lastY)*.006));state.lastX=e.clientX;state.lastY=e.clientY;draw();});
  canvas.addEventListener('pointerup',()=>state.dragging=false);canvas.addEventListener('pointercancel',()=>state.dragging=false);
  canvas.addEventListener('wheel',e=>{e.preventDefault();state.zoom=Math.max(.2,Math.min(8,state.zoom*Math.exp(-e.deltaY*.001)));draw();},{passive:false});
  canvas.addEventListener('dblclick',fitView);
  window.addEventListener('resize',draw);
  const dropzone=$('dropzone');
  for(const name of ['dragenter','dragover'])dropzone.addEventListener(name,e=>{e.preventDefault();dropzone.classList.add('drag');});
  for(const name of ['dragleave','drop'])dropzone.addEventListener(name,e=>{e.preventDefault();dropzone.classList.remove('drag');});
  dropzone.addEventListener('drop',e=>{const files=e.dataTransfer.files;if(files.length){$('fileInput').files=files;$('fileLabel').textContent=files[0].name;updateSequenceGeneratorState();}});
  $('fileInput').addEventListener('change',e=>{$('fileLabel').textContent=e.target.files[0]?.name||'拖入 IFC 文件或点击选择';updateSequenceGeneratorState();});

  updateSequenceGeneratorState();checkHealth();refreshTasks();requestAnimationFrame(animate);
})();

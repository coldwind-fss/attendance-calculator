const escape = value => String(value ?? '').replace(/[&<>"']/g, character => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
})[character]);
const json = value => JSON.stringify(value).replace(/</g, '\\u003c');

function page(title, content, config, controller) {
  return `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${escape(title)}</title><style>body{margin:0;background:#f1f5f9;color:#172554;font:16px system-ui}main{max-width:1000px;margin:24px auto;padding:24px;background:white}table{width:100%;border-collapse:collapse}th,td{text-align:left;border-bottom:1px solid #e2e8f0;padding:8px;vertical-align:top}label{display:block;margin:8px 0}input,button,textarea{font:inherit;padding:6px;box-sizing:border-box}input[type=number]{width:6rem}textarea{width:100%;min-width:7rem}canvas{width:90px;height:90px;border:1px solid #94a3b8;touch-action:none}button{cursor:pointer}#status{min-height:1.5em;white-space:pre-wrap}</style>
<main>${content}<p id="status" role="status"></p></main><script>(${controller.toString()})(${json(config)});</script></html>`;
}

function heading(title, employee, month) {
  return `<h1>${escape(title)}</h1><p>${escape(month)} · ${escape(employee.realName || employee.name)} · ${escape(employee.dept)}</p>`;
}

function scoreForm(config) {
  const form = document.querySelector('form');
  const status = document.getElementById('status');
  form.onsubmit = async event => {
    event.preventDefault();
    const button = form.querySelector('button');
    const details = [...form.querySelectorAll('tr[data-seq]')].map(row => ({
      seq: Number(row.dataset.seq), score: row.querySelector('input').valueAsNumber, remark: row.querySelector('textarea').value,
    }));
    const total = details.reduce((sum, item) => sum + item.score, 0);
    if (total > 120) { status.textContent = '总分不得超过 120 分'; return; }
    button.disabled = true;
    try {
      const response = await fetch(config.base + '/submit-' + config.role, {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({empId: config.empId, month: config.month,
          actionToken: config.token, [config.role + 'Scores']: details, [config.role + 'Score']: total})});
      const result = await response.json();
      if (!response.ok || result.success === false) throw new Error(result.error || '提交失败');
      status.textContent = '已提交评分';
    } catch (error) { status.textContent = error.message; button.disabled = false; }
  };
}

function scoringPage({ employee, units, previous, role, base, token }) {
  const bp = role === 'bp';
  const title = bp ? 'BP 核准' : '上级评分';
  const rows = units.map(unit => {
    const manager = previous.find(score => Number(score.seq) === Number(unit.seq));
    return `<tr data-seq="${Number(unit.seq)}"><td>${escape(unit.indicator)}<br>${escape(unit.rule)}</td>
<td>${escape(unit.completion)}</td><td>${escape(unit.score)}</td>${bp ? `<td>${escape(manager?.score)}<br>${escape(manager?.remark)}</td>` : ''}
<td><input type="number" min="0" step="1" aria-label="本项评分" required></td><td><textarea maxlength="500" aria-label="评分备注"></textarea></td></tr>`;
  }).join('');
  return page(title, `${heading(title, employee, employee.month)}<form><table><thead><tr><th>考核项</th><th>完成情况</th><th>自评</th>${bp ? '<th>上级评分</th>' : ''}<th>评分</th><th>备注</th></tr></thead><tbody>${rows}</tbody></table><p><button>提交评分</button></p></form>`,
    { empId: employee.empId, month: employee.month, role, base, token }, scoreForm);
}

function signatureForm(config) {
  const characters = Array.from(config.realName.replace(/[\s()（）·+＋,，.。_\-—/\\]/g, ''));
  const status = document.getElementById('status');
  const slots = document.getElementById('slots');
  const state = [];
  let challengeId = '';
  const post = async (route, data) => {
    const response = await fetch(config.base + '/' + route, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});
    const result = await response.json();
    if (!response.ok || result.success === false) throw new Error(result.error || '操作失败');
    return result;
  };
  const run = (button, action) => async () => {
    button.disabled = true;
    status.textContent = '';
    try { await action(); } catch (error) { status.textContent = error.message; }
    finally { button.disabled = false; }
  };
  for (const character of characters) {
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = 96;
    canvas.setAttribute('aria-label', '手写 ' + character);
    slots.append(canvas);
    const ctx = canvas.getContext('2d');
    ctx.lineWidth = 3;
    ctx.lineCap = 'round';
    const stats = {canvas, character, points: 0, strokes: 0, length: 0};
    state.push(stats);
    let last;
    const point = event => { const box = canvas.getBoundingClientRect(); return [(event.clientX - box.left) * 96 / box.width, (event.clientY - box.top) * 96 / box.height]; };
    canvas.onpointerdown = event => { canvas.setPointerCapture(event.pointerId); last = point(event); stats.strokes++; stats.points++; };
    canvas.onpointerup = canvas.onpointercancel = () => { last = undefined; };
    canvas.onpointermove = event => {
      if (!last) return;
      const next = point(event);
      if (next.some(value => value < 0 || value >= 96)) { last = undefined; return; }
      ctx.beginPath(); ctx.moveTo(...last); ctx.lineTo(...next); ctx.stroke();
      stats.length += Math.hypot(next[0] - last[0], next[1] - last[1]); stats.points++; last = next;
    };
  }
  document.getElementById('clear').onclick = () => state.forEach(item => {
    item.canvas.getContext('2d').clearRect(0, 0, 96, 96); item.points = item.strokes = item.length = 0;
  });
  const request = document.getElementById('otp');
  request.onclick = run(request, async () => {
    const prefix = new URL(config.base, location.origin).pathname.replace(/\/$/, '');
    const response = await post('request-sign-otp', {empId: config.empId, month: config.month, documentType: config.type,
      pagePath: location.pathname.slice(prefix.length), pageToken: new URLSearchParams(location.search).get('token')});
    challengeId = response.challengeId;
    status.textContent = '验证码已发送，请查看钉钉';
  });
  const submit = document.getElementById('sign');
  submit.onclick = run(submit, async () => {
    const code = document.getElementById('code').value.trim();
    if (!challengeId || !/^\d{6}$/.test(code)) throw new Error('请获取并填写六位钉钉验证码');
    const combined = document.createElement('canvas');
    combined.width = 96 * state.length; combined.height = 96;
    const metrics = state.map((item, index) => {
      const pixels = item.canvas.getContext('2d').getImageData(0, 0, 96, 96).data;
      let minX = 96, minY = 96, maxX = 0, maxY = 0, inkPixels = 0;
      for (let p = 0; p < 96 * 96; p++) if (pixels[4 * p + 3]) {
        const x = p % 96, y = Math.floor(p / 96);
        minX = Math.min(minX, x); minY = Math.min(minY, y); maxX = Math.max(maxX, x); maxY = Math.max(maxY, y); inkPixels++;
      }
      if (inkPixels < 35 || maxX - minX < 8 || maxY - minY < 10) throw new Error('请逐字写清签名');
      combined.getContext('2d').drawImage(item.canvas, 96 * index, 0);
      return {character: item.character, inkPixels, widthRatio: (maxX - minX + 1) / 96, heightRatio: (maxY - minY + 1) / 96};
    });
    const strokeCount = state.reduce((sum, item) => sum + item.strokes, 0);
    const pointCount = state.reduce((sum, item) => sum + item.points, 0);
    const pathLength = state.reduce((sum, item) => sum + item.length, 0);
    if (strokeCount < Math.max(3, state.length * 2) || pointCount < state.length * 3 || pathLength < state.length * 35) throw new Error('手写笔迹过少，请逐字补全');
    const {verificationToken} = await post('verify-sign-otp', {challengeId, code});
    const sigData = combined.toDataURL();
    const documentContent = document.getElementById('document').cloneNode(true);
    const image = document.createElement('img');
    image.src = sigData; image.alt = '员工手写签名'; image.style.maxWidth = '100%'; documentContent.append(image);
    const doc = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>绩效签署记录</title><body>' + documentContent.outerHTML + '</body></html>';
    await post(config.type === 'result' ? 'submit-result' : 'submit-kpi', {empId: config.empId, month: config.month,
      doc, sigData, verificationToken, signatureText: config.realName, signatureFormat: 'realName', signatureStyle: '手写楷体',
      signatureMetrics: {manuallyDrawn: true, signatureFormat: 'realName', expectedCharacterCount: characters.length, characters: metrics,
        crossSlotStrokeCount: 0, strokeCount, pointCount, pathLength}});
    status.textContent = '签署成功';
    document.getElementById('signature').hidden = true;
  });
}

function signingPage({ employee, month, kpis, result, base }) {
  const title = result ? '绩效结果确认' : 'KPI 目标确认';
  const rows = kpis.map(item => `<tr><td>${escape(item.indicator)}</td><td>${escape(item.rule)}</td><td>${escape(item.dataSource || item.source)}</td><td>${escape(item.weight ?? item.max)}</td>${result ? `<td>${escape(item.completion)}</td><td>${escape(item.selfScore)} / ${escape(item.mgrScore)} / ${escape(item.bpScore)}</td>` : ''}</tr>`).join('');
  const table = `<table><thead><tr><th>考核项</th><th>评分规则</th><th>数据来源</th><th>权重</th>${result ? '<th>完成情况</th><th>自评 / 上级 / BP</th>' : ''}</tr></thead><tbody>${rows}</tbody></table>`;
  return page(title, `<article id="document">${heading(title, employee, month)}${table}${result ? `<p>核定 ${escape(result.score)} 分 · ${escape(result.grade)} · 系数 ${escape(result.coeff)}</p>` : ''}</article>
<section id="signature"><h2>身份验证与手写签名</h2><p>请在分字框内用楷体逐字书写真实姓名。</p><div id="slots"></div><p><button id="clear">清除签名</button> <button id="otp">获取钉钉验证码</button></p><label>验证码 <input id="code" inputmode="numeric" maxlength="6" autocomplete="one-time-code"></label><button id="sign">确认并签署</button></section>`,
    {base, empId: employee.empId, month, type: result ? 'result' : 'kpi', realName: employee.realName || ''}, signatureForm);
}

module.exports = { scoringPage, signingPage };

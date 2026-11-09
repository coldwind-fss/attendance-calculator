const { createServer } = require('node:http');
const { timingSafeEqual } = require('node:crypto');
const { spawnSync } = require('node:child_process');
const path = require('node:path');

const internalKey = Buffer.from(process.env.HR_BRIDGE_KEY || '');
if (!internalKey.length) process.exit(2);

// The original service logs request URLs, including signed links. Keep the
// adapter's stdout reserved for its readiness handshake.
console.log = () => {};
const { handleRequest, createAdminSession, accessProfileForName, setOkrTransition, setKpiScoring } = require('./adapters/performance/eval-server.js');

function runRules(moduleName, input) {
  const transition = spawnSync(process.env.HR_BRIDGE_PYTHON, ['-m', moduleName], {
    cwd: path.resolve(__dirname, '..'), input: JSON.stringify(input),
    encoding: 'utf8', timeout: 5000, windowsHide: true,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8' }
  });
  if (transition.status !== 0) throw new Error('绩效计算暂不可用，请稍后重试');
  const payload = JSON.parse(transition.stdout);
  if (payload.error) throw new Error(payload.error);
  return payload.result;
}

setOkrTransition(input => {
  const { actionToken, verificationToken, ...fields } = input.data;
  const { targetSignature, resultSignature, ...record } = input.record;
  return runRules('backend.okr_transitions', { ...input, data: fields, record });
});
setKpiScoring(input => runRules('backend.kpi_scoring', input));

const server = createServer((request, response) => {
  const supplied = Buffer.from(request.headers['x-hr-bridge-key'] || '');
  if (supplied.length !== internalKey.length || !timingSafeEqual(supplied, internalKey)) {
    response.writeHead(404).end();
    return;
  }
  delete request.headers.cookie;
  if (request.headers['x-hr-bridge-scope'] === 'admin') {
    request.headers.cookie = `perf_admin_session=${encodeURIComponent(createAdminSession())}`;
  } else if (request.headers['x-hr-bridge-scope'] === 'performance') {
    const name = Buffer.from(request.headers['x-hr-bridge-actor'] || '', 'base64url').toString('utf8');
    const profile = accessProfileForName(name);
    if (!profile.authorized) {
      response.writeHead(403).end();
      return;
    }
    request.headers.cookie = `perf_admin_session=${encodeURIComponent(createAdminSession(profile))}`;
  }
  Promise.resolve(handleRequest(request, response)).catch(() => {
    if (!response.headersSent) response.writeHead(500, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({ error: '绩效兼容服务处理失败' }));
  });
});

process.stdin.resume();
process.stdin.once('end', () => process.exit(0));
server.listen(0, '127.0.0.1', () => process.stdout.write(`${server.address().port}\n`));

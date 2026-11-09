import { getDingTalkAccessToken, loadDingTalkConfig } from './dingtalkCore.js';

const ROBOT_API_BASE = 'https://api.dingtalk.com/v1.0';

export async function sendRobotMarkdown({ userIds, title, text }) {
  const config = await loadDingTalkConfig();
  if (!config.robotCode) throw new Error('钉钉机器人未配置：请在统一配置文件中设置 robotCode');
  const recipients = [...new Set((userIds || []).map(value => String(value || '').trim()).filter(Boolean))];
  if (!recipients.length) throw new Error('未提供机器人消息接收人');

  const accessToken = await getDingTalkAccessToken(config);
  const response = await fetch(`${ROBOT_API_BASE}/robot/oToMessages/batchSend`, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'x-acs-dingtalk-access-token': accessToken
    },
    body: JSON.stringify({
      robotCode: config.robotCode,
      userIds: recipients,
      msgKey: 'sampleMarkdown',
      msgParam: JSON.stringify({ title, text })
    })
  });
  const payload = await readJson(response);
  if (!response.ok) {
    throw new Error(`钉钉机器人发送失败：${payload?.message || payload?.errmsg || response.status}`);
  }
  return {
    invalidUserIds: payload?.invalidUserIds || payload?.invalid_user_ids || [],
    processQueryKey: payload?.processQueryKey || payload?.process_query_key || ''
  };
}

// processQueryKey 只表示发送请求已被钉钉受理；通过该接口才能区分“投递成功”和“已读”。
export async function queryRobotMessageStatus(processQueryKey) {
  const key = String(processQueryKey || '').trim();
  if (!key) throw new Error('缺少钉钉消息 processQueryKey，无法查询投递状态');
  const config = await loadDingTalkConfig();
  if (!config.robotCode) throw new Error('钉钉机器人未配置：请在统一配置文件中设置 robotCode');
  const accessToken = await getDingTalkAccessToken(config);
  const url = new URL(`${ROBOT_API_BASE}/robot/oToMessages/readStatus`);
  url.searchParams.set('processQueryKey', key);
  url.searchParams.set('robotCode', config.robotCode);
  const response = await fetch(url, {
    headers: { 'x-acs-dingtalk-access-token': accessToken }
  });
  const payload = await readJson(response);
  if (!response.ok) {
    throw new Error(`查询钉钉投递状态失败：${payload?.message || payload?.errmsg || response.status}`);
  }
  return {
    sendStatus: String(payload?.sendStatus || payload?.send_status || '').toUpperCase(),
    messageReadInfoList: Array.isArray(payload?.messageReadInfoList)
      ? payload.messageReadInfoList
      : (Array.isArray(payload?.message_read_info_list) ? payload.message_read_info_list : [])
  };
}

export async function verifyFulltimeNotificationAdminToken(candidate) {
  const config = await loadDingTalkConfig();
  return secureEqual(String(candidate || ''), String(config.adminDebugToken || ''));
}

export async function getFulltimeAttendanceReviewerUserId() {
  const config = await loadDingTalkConfig();
  const userId = String(config.fulltimeAttendanceReviewerUserId || '').trim();
  if (!userId) throw new Error('考勤复核接收人未配置：请设置 fulltimeAttendanceReviewerUserId');
  return userId;
}

async function readJson(response) {
  const text = await response.text();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return { message: text.slice(0, 500) };
  }
}

function secureEqual(left, right) {
  if (!left || !right || left.length !== right.length) return false;
  let diff = 0;
  for (let index = 0; index < left.length; index += 1) diff |= left.charCodeAt(index) ^ right.charCodeAt(index);
  return diff === 0;
}

/**
 * Operator-facing error translation: raw backend/HTTP errors arrive as English
 * driver strings (RealSense, CAN, pyserial, AIM SDK) or bare HTTP statuses,
 * and are mapped here to readable Chinese plus a suggested action. The raw
 * detail always stays visible in parentheses for debugging.
 */

const RAW_DETAIL_LIMIT = 160;

type ErrorRule = { pattern: RegExp; text: string };

// First match wins. Backend 422 detail written in Chinese never matches these
// patterns and passes through untouched below.
const RULES: ErrorRule[] = [
  {
    pattern: /frame didn't arrive|d435|camera/i,
    text: '相机取帧失败：可能是 USB 接触不良，系统会自动重试恢复；持续失败请检查相机线缆',
  },
  {
    pattern: /inverse kinematics|did not converge|movepbyj|movel/i,
    text: '目标位置机械臂无法到达：请检查该步骤的偏移量和姿态参数',
  },
  {
    pattern:
      /input\/output error|write failed|multiple access|reports readiness/i,
    text: '设备通讯中断：串口设备可能重新枚举，系统会自动重连；持续失败请检查接线',
  },
  {
    pattern: /rod.*(timed out|timeout)|read_angle|推杆/i,
    text: '推杆无应答：请确认推杆控制板已上电、串口通讯已开启',
  },
  {
    pattern: /lift|aim_|did not respond|升降台/i,
    text: '升降台无应答：请检查升降台控制器供电与串口连接',
  },
  {
    pattern: /thermal|温度/i,
    text: '温度保护已触发：机械臂温度超过阈值，请等待冷却后按提示解除锁定',
  },
  {
    pattern: /already running|正忙|busy/i,
    text: '有正在执行的任务或动作，请等待结束后再试',
  },
  {
    pattern: /503|service unavailable|服务不可用|不可用/i,
    text: '本机服务暂时不可用，请稍后重试或检查后端状态',
  },
  {
    // 只匹配浏览器/HTTP 客户端的网络层失败。车端/机械臂作业超时的原文
    //（如 "timed out"）不是连接故障，交给下方的中文直通/兜底逻辑展示。
    pattern: /failed to fetch|networkerror|network/i,
    text: '无法连接服务器：请确认后端已启动',
  },
];

function hasChinese(text: string): boolean {
  return /[一-鿿]/.test(text);
}

function rawText(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === 'string') return error;
  return '';
}

function truncate(text: string): string {
  return text.length > RAW_DETAIL_LIMIT
    ? `${text.slice(0, RAW_DETAIL_LIMIT)}…`
    : text;
}

/**
 * Translate `error` for operators; `fallback` is the caller's context phrase
 * (e.g. '槽位执行失败'). Backend detail written in Chinese passes through
 * unchanged; anything else keeps the original text appended for debugging.
 */
export function errorMessage(error: unknown, fallback: string): string {
  const raw = rawText(error);
  if (!raw) return fallback;
  for (const rule of RULES) {
    if (rule.pattern.test(raw)) {
      return `${rule.text}（原文：${truncate(raw)}）`;
    }
  }
  if (hasChinese(raw)) return raw;
  return `${fallback}（原文：${truncate(raw)}）`;
}

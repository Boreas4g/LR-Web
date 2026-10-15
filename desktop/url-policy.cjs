'use strict';

const LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]']);

function normalizeStationUrl(input) {
  const rawValue = String(input ?? '').trim();
  if (!rawValue) {
    throw new Error('请输入控制站地址。');
  }

  const value = /^[a-z][a-z\d+.-]*:\/\//i.test(rawValue)
    ? rawValue
    : `https://${rawValue}`;
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new Error('控制站地址格式不正确。');
  }

  if (url.username || url.password) {
    throw new Error('控制站地址不能包含账号或密码。');
  }

  const isLoopbackHttp =
    url.protocol === 'http:' && LOOPBACK_HOSTS.has(url.hostname.toLowerCase());
  if (url.protocol !== 'https:' && !isLoopbackHttp) {
    throw new Error('局域网控制站必须使用 HTTPS；HTTP 仅用于本机调试。');
  }

  return url.origin;
}

function isSameOrigin(candidate, origin) {
  try {
    return new URL(candidate).origin === origin;
  } catch {
    return false;
  }
}

module.exports = { isSameOrigin, normalizeStationUrl };

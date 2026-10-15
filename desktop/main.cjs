'use strict';

const fs = require('node:fs');
const path = require('node:path');
const dgram = require('node:dgram');
const { fileURLToPath, pathToFileURL } = require('node:url');
const {
  app,
  BrowserWindow,
  dialog,
  ipcMain,
  Menu,
  net,
  session,
  shell,
} = require('electron');
const { isSameOrigin, normalizeStationUrl } = require('./url-policy.cjs');
const {
  parseDiscoveryAnnouncement,
  parseStationHealth,
} = require('./discovery.cjs');

const APP_NAME = '智巡机器人调度';
const HEALTH_TIMEOUT_MS = 6000;
const DISCOVERY_ADDRESS = '239.255.77.77';
const DISCOVERY_PORT = 39777;
const DISCOVERY_STALE_MS = 15_000;
const SETUP_FILE = path.join(__dirname, 'setup.html');
const SETUP_URL = pathToFileURL(SETUP_FILE).toString();

let mainWindow = null;
let currentMode = 'setup';
let configuredOrigin = null;
let setupError = '';
let logFile = '';
let discoverySocket = null;
const discoveredStations = new Map();

app.setName(APP_NAME);

function writeLog(message) {
  const line = `${new Date().toISOString()} ${message}\n`;
  if (!logFile) {
    return;
  }
  try {
    fs.appendFileSync(logFile, line, { encoding: 'utf8', mode: 0o600 });
  } catch {
    // Logging must never prevent the operator from opening the application.
  }
}

function configPath() {
  return path.join(app.getPath('userData'), 'config.json');
}

function loadConfig() {
  try {
    const parsed = JSON.parse(fs.readFileSync(configPath(), 'utf8'));
    return {
      stationUrl: normalizeStationUrl(parsed.stationUrl),
      stationId:
        typeof parsed.stationId === 'string' && parsed.stationId.length <= 120
          ? parsed.stationId
          : undefined,
    };
  } catch (error) {
    if (error?.code !== 'ENOENT') {
      writeLog(`Unable to load desktop configuration: ${error.message}`);
    }
    return null;
  }
}

function saveConfig(stationUrl, stationId) {
  const destination = configPath();
  const temporary = `${destination}.tmp`;
  fs.mkdirSync(path.dirname(destination), { recursive: true, mode: 0o700 });
  fs.writeFileSync(
    temporary,
    `${JSON.stringify({ stationUrl, stationId }, null, 2)}\n`,
    {
      encoding: 'utf8',
      mode: 0o600,
    },
  );
  fs.renameSync(temporary, destination);
  try {
    fs.chmodSync(destination, 0o600);
  } catch {
    // Windows does not implement POSIX file modes.
  }
}

function friendlyConnectionError(error) {
  const message = String(error?.message ?? error);
  if (/ERR_CERT_AUTHORITY_INVALID/i.test(message)) {
    return '控制站证书不受信任，请安装维护人员提供的控制站 CA 后重试。';
  }
  if (/ERR_CERT_DATE_INVALID|certificate.*expired|证书.*过期/i.test(message)) {
    return '控制站证书已过期或系统时间不正确，请联系维护人员处理。';
  }
  if (/ERR_CERT_COMMON_NAME_INVALID/i.test(message)) {
    return '控制站证书与输入的地址不匹配，请使用维护人员提供的地址。';
  }
  if (/CERT_|certificate|证书/i.test(message)) {
    return '控制站证书校验失败，请检查地址、系统时间和 CA 安装状态。';
  }
  if (/aborted|timeout/i.test(message)) {
    return '连接控制站超时，请检查地址、网络和控制站服务。';
  }
  return '无法连接控制站，请检查地址、网络和控制站服务。';
}

async function checkStation(
  origin,
  expectedStationId,
  allowLegacyStation = false,
) {
  const abortController = new AbortController();
  const timeout = setTimeout(() => abortController.abort(), HEALTH_TIMEOUT_MS);
  try {
    const response = await net.fetch(new URL('/health', origin).toString(), {
      cache: 'no-store',
      signal: abortController.signal,
    });
    if (!response.ok) {
      throw new Error(`Health endpoint returned HTTP ${response.status}`);
    }
    const health = await response.json();
    const station = parseStationHealth(health, expectedStationId);
    if (station) return station;
    // Preserve existing trusted HTTPS bindings through a rolling upgrade. New
    // manual and discovered connections always require station identity.
    if (
      allowLegacyStation &&
      !expectedStationId &&
      health &&
      typeof health === 'object' &&
      typeof health.service === 'string'
    ) {
      return null;
    }
    throw new Error('Health endpoint returned an unexpected station identity');
  } finally {
    clearTimeout(timeout);
  }
}

function stationList() {
  const now = Date.now();
  for (const [id, station] of discoveredStations) {
    if (now - station.seenAt > DISCOVERY_STALE_MS)
      discoveredStations.delete(id);
  }
  return [...discoveredStations.values()]
    .sort((left, right) => left.name.localeCompare(right.name, 'zh-CN'))
    .map(({ id, name, url }) => ({ id, name, url }));
}

function publishStations() {
  if (mainWindow && !mainWindow.isDestroyed() && currentMode === 'setup') {
    mainWindow.webContents.send('desktop:stations-changed', stationList());
  }
}

function startDiscovery() {
  if (discoverySocket) return;
  const socket = dgram.createSocket({ type: 'udp4', reuseAddr: true });
  socket.on('error', (error) => {
    writeLog(`Control station discovery error: ${error.message}`);
  });
  socket.on('message', (message) => {
    const station = parseDiscoveryAnnouncement(message);
    if (!station) return;
    const previous = discoveredStations.get(station.id);
    discoveredStations.set(station.id, { ...station, seenAt: Date.now() });
    if (
      !previous ||
      previous.url !== station.url ||
      previous.name !== station.name
    ) {
      publishStations();
    }
  });
  socket.bind(DISCOVERY_PORT, () => {
    try {
      socket.addMembership(DISCOVERY_ADDRESS);
      discoverySocket = socket;
      writeLog('Control station discovery started');
    } catch (error) {
      writeLog(`Control station discovery unavailable: ${error.message}`);
      socket.close();
    }
  });
}

function stopDiscovery() {
  if (!discoverySocket) return;
  discoverySocket.close();
  discoverySocket = null;
}

function isSetupSender(event) {
  try {
    return fileURLToPath(event.senderFrame.url) === SETUP_FILE;
  } catch {
    return false;
  }
}

async function showSetup(errorMessage = '') {
  currentMode = 'setup';
  setupError = errorMessage;
  await mainWindow.loadFile(SETUP_FILE);
  publishStations();
}

async function showStation(origin) {
  configuredOrigin = origin;
  currentMode = 'remote';
  setupError = '';
  writeLog(`Opening control station ${origin}`);
  await mainWindow.loadURL(origin);
}

async function openConfiguredStation() {
  const config = loadConfig();
  if (!config) {
    await showSetup();
    return;
  }

  configuredOrigin = config.stationUrl;
  try {
    const station = await checkStation(
      config.stationUrl,
      config.stationId,
      true,
    );
    if (station) saveConfig(config.stationUrl, station.id);
    await showStation(config.stationUrl);
  } catch (error) {
    writeLog(`Control station check failed: ${error.message}`);
    await showSetup(friendlyConnectionError(error));
  }
}

function installNavigationPolicy(window) {
  window.webContents.on('will-navigate', (event, targetUrl) => {
    const allowRemote =
      currentMode === 'remote' && isSameOrigin(targetUrl, configuredOrigin);
    const allowSetup = currentMode === 'setup' && targetUrl === SETUP_URL;
    if (allowRemote || allowSetup) {
      return;
    }
    event.preventDefault();
    if (/^https?:/i.test(targetUrl)) {
      void shell.openExternal(targetUrl);
    }
  });

  window.webContents.setWindowOpenHandler(({ url }) => {
    if (currentMode === 'remote' && isSameOrigin(url, configuredOrigin)) {
      void window.loadURL(url);
    } else if (/^https?:/i.test(url)) {
      void shell.openExternal(url);
    }
    return { action: 'deny' };
  });

  window.webContents.on('will-attach-webview', (event) =>
    event.preventDefault(),
  );
  window.webContents.on(
    'did-fail-load',
    (_event, errorCode, errorDescription, validatedUrl, isMainFrame) => {
      if (!isMainFrame || errorCode === -3 || currentMode !== 'remote') {
        return;
      }
      writeLog(
        `Page load failed (${errorCode}) ${validatedUrl}: ${errorDescription}`,
      );
      void showSetup(friendlyConnectionError(new Error(errorDescription)));
    },
  );
  window.webContents.on('render-process-gone', (_event, details) => {
    writeLog(`Renderer exited: ${details.reason}`);
    if (!window.isDestroyed()) {
      void showSetup('界面进程已重新启动，请重新连接控制站。');
    }
  });
}

function createWindow() {
  const icon = app.isPackaged
    ? path.join(process.resourcesPath, 'icon.png')
    : path.join(__dirname, 'assets', 'icon.png');
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 960,
    minHeight: 640,
    show: false,
    backgroundColor: '#f5f5f5',
    icon,
    title: APP_NAME,
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
      spellcheck: false,
    },
  });

  installNavigationPolicy(mainWindow);
  mainWindow.once('ready-to-show', () => mainWindow?.show());
  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

function configureMenu() {
  const template = [
    {
      label: '应用',
      submenu: [
        {
          label: '配置控制站',
          click: () => void showSetup(),
        },
        { type: 'separator' },
        { role: 'quit', label: '退出' },
      ],
    },
    {
      label: '视图',
      submenu: [
        { role: 'reload', label: '重新加载' },
        { role: 'resetZoom', label: '重置缩放' },
        { role: 'togglefullscreen', label: '全屏' },
      ],
    },
    {
      label: '帮助',
      submenu: [
        {
          label: '打开日志目录',
          click: () => void shell.openPath(app.getPath('logs')),
        },
        {
          label: '支持信息',
          click: () => {
            void dialog.showMessageBox(mainWindow, {
              type: 'info',
              title: '支持信息',
              message: `${APP_NAME} ${app.getVersion()}`,
              detail: `控制站：${configuredOrigin ?? '未配置'}\n配置目录：${app.getPath('userData')}\n日志目录：${app.getPath('logs')}`,
            });
          },
        },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

ipcMain.handle('desktop:get-state', (event) => {
  if (!isSetupSender(event)) {
    throw new Error('Forbidden IPC origin');
  }
  return {
    stationUrl: configuredOrigin ?? '',
    stations: stationList(),
    error: setupError,
    version: app.getVersion(),
  };
});

ipcMain.handle('desktop:refresh-discovery', (event) => {
  if (!isSetupSender(event)) {
    throw new Error('Forbidden IPC origin');
  }
  return stationList();
});

ipcMain.handle('desktop:connect', async (event, candidate) => {
  if (!isSetupSender(event)) {
    throw new Error('Forbidden IPC origin');
  }
  let origin;
  try {
    origin = normalizeStationUrl(candidate);
  } catch (error) {
    return { ok: false, error: error.message };
  }
  try {
    const station = await checkStation(origin);
    saveConfig(origin, station.id);
    configuredOrigin = origin;
    setTimeout(() => void showStation(origin), 100);
    return { ok: true };
  } catch (error) {
    writeLog(`Connection request failed: ${error.message}`);
    return { ok: false, error: friendlyConnectionError(error) };
  }
});

ipcMain.handle('desktop:connect-discovered', async (event, stationId) => {
  if (!isSetupSender(event)) {
    throw new Error('Forbidden IPC origin');
  }
  const station = discoveredStations.get(String(stationId));
  if (!station || Date.now() - station.seenAt > DISCOVERY_STALE_MS) {
    return { ok: false, error: '该控制站已离线，请刷新后重试或联系维护人员。' };
  }
  try {
    const healthStation = await checkStation(station.url, station.id);
    saveConfig(station.url, healthStation.id);
    configuredOrigin = station.url;
    setTimeout(() => void showStation(station.url), 100);
    return { ok: true };
  } catch (error) {
    writeLog(`Discovered station connection failed: ${error.message}`);
    return { ok: false, error: friendlyConnectionError(error) };
  }
});

const hasSingleInstanceLock = app.requestSingleInstanceLock();
if (!hasSingleInstanceLock) {
  app.quit();
} else if (app.commandLine.hasSwitch('no-sandbox')) {
  app.whenReady().then(() => {
    dialog.showErrorBox(
      '无法安全启动',
      '当前系统试图关闭 Electron 安全沙箱。请安装 deb 版本，或由维护人员启用系统用户命名空间。',
    );
    app.exit(1);
  });
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) {
        mainWindow.restore();
      }
      mainWindow.focus();
    }
  });

  app.whenReady().then(async () => {
    app.setAppLogsPath();
    fs.mkdirSync(app.getPath('logs'), { recursive: true, mode: 0o700 });
    logFile = path.join(app.getPath('logs'), 'main.log');
    session.defaultSession.setPermissionCheckHandler(() => false);
    session.defaultSession.setPermissionRequestHandler(
      (_webContents, _permission, callback) => callback(false),
    );
    createWindow();
    configureMenu();
    startDiscovery();
    await openConfiguredStation();

    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) {
        createWindow();
        void openConfiguredStation();
      }
    });
  });

  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') {
      app.quit();
    }
  });

  app.on('before-quit', stopDiscovery);
}

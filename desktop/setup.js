const form = document.querySelector('#station-form');
const stationUrl = document.querySelector('#station-url');
const connectButton = document.querySelector('#connect-button');
const connectionError = document.querySelector('#connection-error');
const connectionStatus = document.querySelector('#connection-status');
const appVersion = document.querySelector('#app-version');
const stationList = document.querySelector('#station-list');
const discoveryStatus = document.querySelector('#discovery-status');
const refreshDiscoveryButton = document.querySelector(
  '#refresh-discovery-button',
);

function setError(message) {
  connectionError.textContent = message;
  connectionError.hidden = !message;
}

function setConnecting(isConnecting, label = '') {
  connectButton.disabled = isConnecting;
  refreshDiscoveryButton.disabled = isConnecting;
  if (label) connectionStatus.textContent = label;
}

function renderStations(stations) {
  stationList.replaceChildren();
  if (!stations.length) {
    discoveryStatus.textContent =
      '暂未发现控制站。请确认电脑与控制站接入同一局域网。';
    return;
  }
  discoveryStatus.textContent = `已发现 ${stations.length} 个控制站。`;
  for (const station of stations) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'station-button';
    const name = document.createElement('strong');
    name.textContent = station.name;
    const address = document.createElement('span');
    address.textContent = station.url;
    button.append(name, address);
    button.addEventListener('click', async () => {
      setConnecting(true, '正在验证控制站');
      setError('');
      try {
        const result = await window.lrDesktop.connectDiscovered(station.id);
        if (!result.ok) {
          setError(result.error);
          setConnecting(false, '连接失败');
        }
      } catch {
        setError('桌面程序暂时无法处理连接请求，请重新启动程序。');
        setConnecting(false, '连接失败');
      }
    });
    stationList.append(button);
  }
}

async function initialize() {
  const state = await window.lrDesktop.getState();
  stationUrl.value = state.stationUrl;
  appVersion.textContent = `v${state.version}`;
  setError(state.error);
  renderStations(state.stations);
  window.lrDesktop.onStationsChanged(renderStations);
  stationUrl.focus();
  stationUrl.select();
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  setConnecting(true, '正在连接');
  setError('');
  try {
    const result = await window.lrDesktop.connect(stationUrl.value);
    if (!result.ok) {
      setError(result.error);
      setConnecting(false, '连接失败');
      stationUrl.focus();
      return;
    }
    connectionStatus.textContent = '连接成功';
  } catch {
    setError('桌面程序暂时无法处理连接请求，请重新启动程序。');
    setConnecting(false, '连接失败');
  }
});

refreshDiscoveryButton.addEventListener('click', async () => {
  refreshDiscoveryButton.disabled = true;
  discoveryStatus.textContent = '正在刷新局域网控制站列表…';
  try {
    renderStations(await window.lrDesktop.refreshDiscovery());
  } catch {
    discoveryStatus.textContent = '无法刷新控制站列表，请重新启动程序。';
  } finally {
    refreshDiscoveryButton.disabled = false;
  }
});

void initialize();

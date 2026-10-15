'use strict';

const { contextBridge, ipcRenderer } = require('electron');

if (window.location.protocol === 'file:') {
  contextBridge.exposeInMainWorld('lrDesktop', {
    connect: (stationUrl) => ipcRenderer.invoke('desktop:connect', stationUrl),
    connectDiscovered: (stationId) =>
      ipcRenderer.invoke('desktop:connect-discovered', stationId),
    getState: () => ipcRenderer.invoke('desktop:get-state'),
    refreshDiscovery: () => ipcRenderer.invoke('desktop:refresh-discovery'),
    onStationsChanged: (listener) => {
      const handler = (_event, stations) => listener(stations);
      ipcRenderer.on('desktop:stations-changed', handler);
      return () =>
        ipcRenderer.removeListener('desktop:stations-changed', handler);
    },
  });
}

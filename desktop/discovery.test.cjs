'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
  parseDiscoveryAnnouncement,
  parseStationHealth,
} = require('./discovery.cjs');

test('accepts a valid HTTPS control-station announcement', () => {
  assert.deepEqual(
    parseDiscoveryAnnouncement(
      JSON.stringify({
        protocol: 'lr-control-station-discovery/v1',
        stationId: 'lr-station-123',
        stationName: '图书馆控制站',
        stationUrl: 'https://control-station.example/path-is-ignored',
      }),
    ),
    {
      id: 'lr-station-123',
      name: '图书馆控制站',
      url: 'https://control-station.example',
    },
  );
});

test('rejects announcements that cannot be trusted through HTTPS checks', () => {
  const base = {
    protocol: 'lr-control-station-discovery/v1',
    stationId: 'lr-station-123',
    stationName: '图书馆控制站',
  };
  assert.equal(
    parseDiscoveryAnnouncement(
      JSON.stringify({ ...base, stationUrl: 'http://192.168.1.10' }),
    ),
    null,
  );
  assert.equal(
    parseDiscoveryAnnouncement(
      JSON.stringify({
        ...base,
        protocol: 'another-protocol',
        stationUrl: 'https://x.test',
      }),
    ),
    null,
  );
  assert.equal(parseDiscoveryAnnouncement('{not-json'), null);
});

test('only accepts the control-station identity returned by health checks', () => {
  const health = {
    status: 'ok',
    service: 'LR Control API',
    station: {
      product: 'lr-control-station',
      id: 'lr-station-123',
      name: '图书馆控制站',
    },
  };
  assert.deepEqual(parseStationHealth(health, 'lr-station-123'), {
    id: 'lr-station-123',
    name: '图书馆控制站',
  });
  assert.equal(parseStationHealth(health, 'another-station'), null);
  assert.equal(parseStationHealth({ service: 'other' }), null);
});

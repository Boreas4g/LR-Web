'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { isSameOrigin, normalizeStationUrl } = require('./url-policy.cjs');

test('normalizes a station host to its HTTPS origin', () => {
  assert.equal(
    normalizeStationUrl('control-station.local/path?unused=1'),
    'https://control-station.local',
  );
  assert.equal(
    normalizeStationUrl('https://192.168.10.10:8443/'),
    'https://192.168.10.10:8443',
  );
});

test('allows plain HTTP only for loopback development', () => {
  assert.equal(
    normalizeStationUrl('http://localhost:8000'),
    'http://localhost:8000',
  );
  assert.equal(
    normalizeStationUrl('http://127.0.0.1:8000'),
    'http://127.0.0.1:8000',
  );
  assert.throws(
    () => normalizeStationUrl('http://192.168.10.10:8000'),
    /必须使用 HTTPS/,
  );
});

test('rejects credentials and unrelated schemes', () => {
  assert.throws(
    () => normalizeStationUrl('https://admin:secret@example.test'),
    /不能包含账号/,
  );
  assert.throws(
    () => normalizeStationUrl('file:///tmp/index.html'),
    /必须使用 HTTPS/,
  );
});

test('compares navigation targets by origin', () => {
  assert.equal(
    isSameOrigin(
      'https://station.test/dispatch/robots',
      'https://station.test',
    ),
    true,
  );
  assert.equal(
    isSameOrigin('https://other.test/', 'https://station.test'),
    false,
  );
});

'use strict';

const { normalizeStationUrl } = require('./url-policy.cjs');

const DISCOVERY_PROTOCOL = 'lr-control-station-discovery/v1';
const MAX_STATION_FIELD_LENGTH = 120;

function parseStationHealth(health, expectedStationId) {
  if (
    !health ||
    typeof health !== 'object' ||
    typeof health.service !== 'string'
  ) {
    return null;
  }
  const station = health.station;
  if (
    !station ||
    typeof station !== 'object' ||
    station.product !== 'lr-control-station' ||
    typeof station.id !== 'string' ||
    !station.id.trim() ||
    station.id.length > MAX_STATION_FIELD_LENGTH ||
    typeof station.name !== 'string' ||
    !station.name.trim() ||
    station.name.length > MAX_STATION_FIELD_LENGTH ||
    (expectedStationId && station.id !== expectedStationId)
  ) {
    return null;
  }
  return { id: station.id, name: station.name };
}

function parseDiscoveryAnnouncement(raw) {
  let announcement;
  try {
    announcement = JSON.parse(
      Buffer.isBuffer(raw) ? raw.toString('utf8') : raw,
    );
  } catch {
    return null;
  }
  if (!announcement || typeof announcement !== 'object') return null;
  if (announcement.protocol !== DISCOVERY_PROTOCOL) return null;
  if (
    typeof announcement.stationId !== 'string' ||
    !announcement.stationId.trim() ||
    announcement.stationId.length > MAX_STATION_FIELD_LENGTH ||
    typeof announcement.stationName !== 'string' ||
    !announcement.stationName.trim() ||
    announcement.stationName.length > MAX_STATION_FIELD_LENGTH ||
    typeof announcement.stationUrl !== 'string'
  ) {
    return null;
  }
  try {
    return {
      id: announcement.stationId.trim(),
      name: announcement.stationName.trim(),
      url: normalizeStationUrl(announcement.stationUrl),
    };
  } catch {
    return null;
  }
}

module.exports = {
  DISCOVERY_PROTOCOL,
  parseDiscoveryAnnouncement,
  parseStationHealth,
};

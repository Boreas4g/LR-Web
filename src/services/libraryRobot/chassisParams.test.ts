import { describe, expect, it } from 'vitest';

import {
  type ChassisParam,
  dirtyChassisValues,
  groupChassisParams,
} from './chassisParams';

function param(partial: Partial<ChassisParam> & { key: string }): ChassisParam {
  return {
    group: '导航速度链',
    label: partial.key,
    unit: 'm/s',
    type: 'float',
    min: 0,
    max: 1,
    step: 0.01,
    value: 0.3,
    ...partial,
  };
}

describe('groupChassisParams', () => {
  it('groups by the group field and keeps first-seen order', () => {
    const params = [
      param({ key: 'nav.max_vel_x' }),
      param({ key: 'chassis.wheel_radius', group: '车辆与轮系' }),
      param({ key: 'nav.max_vel_theta' }),
      param({ key: 'chassis.track_width', group: '车辆与轮系' }),
      param({ key: 'amcl.max_particles', group: 'AMCL', type: 'int' }),
    ];
    const groups = groupChassisParams(params);
    expect(groups.map((item) => item.group)).toEqual([
      '导航速度链',
      '车辆与轮系',
      'AMCL',
    ]);
    expect(groups[0].params.map((item) => item.key)).toEqual([
      'nav.max_vel_x',
      'nav.max_vel_theta',
    ]);
    expect(groups[1].params.map((item) => item.key)).toEqual([
      'chassis.wheel_radius',
      'chassis.track_width',
    ]);
  });

  it('returns an empty list for no params', () => {
    expect(groupChassisParams([])).toEqual([]);
  });
});

describe('dirtyChassisValues', () => {
  const params = [
    param({ key: 'nav.max_vel_x', value: 0.3 }),
    param({ key: 'collision.min_points', type: 'int', value: 4 }),
    param({ key: 'some.flag', type: 'bool', value: false }),
  ];

  it('is empty without edits or with unchanged values', () => {
    expect(dirtyChassisValues(params, {})).toEqual({});
    expect(dirtyChassisValues(params, { 'nav.max_vel_x': 0.3 })).toEqual({});
  });

  it('collects only changed values, including booleans', () => {
    expect(
      dirtyChassisValues(params, {
        'nav.max_vel_x': 0.45,
        'collision.min_points': 4,
        'some.flag': true,
      }),
    ).toEqual({ 'nav.max_vel_x': 0.45, 'some.flag': true });
  });

  it('ignores draft keys that are not loaded parameters', () => {
    expect(dirtyChassisValues(params, { 'unknown.key': 1 })).toEqual({});
  });
});

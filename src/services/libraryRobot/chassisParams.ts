import { apiRequest } from './index';

/** One tunable chassis/navigation parameter as exposed by the control API. */
export type ChassisParam = {
  key: string;
  group: string;
  label: string;
  unit: string;
  type: 'float' | 'int' | 'bool';
  min: number;
  max: number;
  step: number;
  /** Current value; null when the backend cannot derive it (e.g. the two
   * costmap footprint levels disagree) — the page then shows an empty input. */
  value: number | boolean | null;
  /** False when the parameter is registered but absent from the current
   * on-robot files; the page disables editing and shows the reason. */
  available?: boolean;
  unavailable_reason?: string | null;
};

export type ChassisParamsResponse = {
  params: ChassisParam[];
};

/** Editable values keyed by parameter key; numbers and booleans only. */
export type ChassisParamValues = Record<string, number | boolean>;

export async function getChassisParams(): Promise<ChassisParamsResponse> {
  return apiRequest('/api/v1/debug/chassis-params', { cache: 'no-store' });
}

/** Validates and persists the given values into the on-robot config files. */
export async function updateChassisParams(
  values: ChassisParamValues,
): Promise<ChassisParamsResponse> {
  return apiRequest('/api/v1/debug/chassis-params', {
    method: 'PUT',
    body: JSON.stringify({ values }),
  });
}

/** Restarts the robot navigation runtime so saved parameters take effect. */
export async function applyChassisParams(): Promise<{ ok: boolean }> {
  return apiRequest('/api/v1/debug/chassis-params/apply', {
    method: 'POST',
    body: JSON.stringify({}),
  });
}

export type ChassisParamGroup = {
  group: string;
  params: ChassisParam[];
};

/** Groups parameters by their `group` field, preserving first-seen order. */
export function groupChassisParams(params: ChassisParam[]): ChassisParamGroup[] {
  const groups: ChassisParamGroup[] = [];
  const byGroup = new Map<string, ChassisParamGroup>();
  for (const param of params) {
    let entry = byGroup.get(param.group);
    if (!entry) {
      entry = { group: param.group, params: [] };
      byGroup.set(param.group, entry);
      groups.push(entry);
    }
    entry.params.push(param);
  }
  return groups;
}

/** Collects the values that differ from the server state (the PUT payload). */
export function dirtyChassisValues(
  params: ChassisParam[],
  draft: ChassisParamValues,
): ChassisParamValues {
  const dirty: ChassisParamValues = {};
  for (const param of params) {
    const edited = draft[param.key];
    if (edited !== undefined && edited !== param.value) {
      dirty[param.key] = edited;
    }
  }
  return dirty;
}

import { apiRequest } from './index';

/** 压测单步取书：把书取到书立的指定槽位；升降高度按书籍登记高度。 */
export type StressTestTakeoutAction = {
  bookId: string;
  destinationSlot: number;
};

/** 压测单步中位还书：从书立槽位取回该书，识别目标书后还到其左边。 */
export type StressTestReturnAction = {
  sourceSlot: number;
  referenceBookId: string;
};

/** 压测单循环配置：导航到目标点 → 按序取书入槽 → 按序中位还书（还到目标书
 * 左边）→ 收拢（失能机械臂 + 降升降台）→ 直退 1.5 米（不走导航）收尾。 */
export type StressTestTemplateConfig = {
  mapId: string;
  targetAnnotationId: string;
  takeouts: StressTestTakeoutAction[];
  returns: StressTestReturnAction[];
  liftSpeedLevel: number;
  maxRounds: number | null;
  stopOnFailure: boolean;
};

/** 一份保存的压测任务模板（按名称复用单循环配置）。 */
export type StressTestTemplate = {
  id: string;
  name: string;
  config: StressTestTemplateConfig;
  createdAt: string;
  updatedAt: string;
};

export type StressTestTemplateUpsert = {
  name: string;
  config: StressTestTemplateConfig;
};

export async function listStressTestTemplates(): Promise<StressTestTemplate[]> {
  return apiRequest('/api/v1/debug/stress-test/templates', { cache: 'no-store' });
}

export async function createStressTestTemplate(
  values: StressTestTemplateUpsert,
): Promise<StressTestTemplate> {
  return apiRequest('/api/v1/debug/stress-test/templates', {
    method: 'POST',
    body: JSON.stringify(values),
  });
}

export async function updateStressTestTemplate(
  templateId: string,
  values: StressTestTemplateUpsert,
): Promise<StressTestTemplate> {
  return apiRequest(`/api/v1/debug/stress-test/templates/${templateId}`, {
    method: 'PUT',
    body: JSON.stringify(values),
  });
}

export async function deleteStressTestTemplate(templateId: string): Promise<void> {
  return apiRequest(`/api/v1/debug/stress-test/templates/${templateId}`, {
    method: 'DELETE',
  });
}

/** 取还书动作序列的静态校验（与后端模板校验规则一致）：返回错误信息或 null。 */
export function validateStressTestActions(
  takeouts: { destinationSlot: number }[],
  returns: { sourceSlot: number }[],
): string | null {
  if (takeouts.length === 0) return '至少需要一步取书动作';
  const slots = takeouts.map((takeout) => takeout.destinationSlot);
  if (new Set(slots).size !== slots.length) return '取书目标槽位不能重复';
  const returnSlots = returns.map((action) => action.sourceSlot);
  if (new Set(returnSlots).size !== returnSlots.length) return '还书槽位不能重复';
  for (const action of returns) {
    if (!slots.includes(action.sourceSlot))
      return `还书槽位 ${action.sourceSlot} 没有对应的取书动作`;
  }
  return null;
}

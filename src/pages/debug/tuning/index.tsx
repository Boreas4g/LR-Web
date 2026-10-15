import { useState } from 'react';

import type { TuningKind } from '@/services/libraryRobot/tuning';
import TuningPanel, {
  type TuningStepDef,
} from '../../circulation/components/TuningPanel';
import { midreturnSteps } from './midreturnSteps';
import { returnSteps } from './returnSteps';
import { takeSteps } from './takeSteps';

type TuningFlow = {
  tab: string;
  pageTitle: string;
  pageContent: string;
  steps: TuningStepDef[];
};

const FLOWS: Record<TuningKind, TuningFlow> = {
  take: {
    tab: '取书',
    pageTitle: '取书调参',
    pageContent:
      '调整取书运动计划参数，并按步骤单步执行验证；保存后立即热生效并持久化到机器人本机服务配置文件。',
    steps: takeSteps,
  },
  return: {
    tab: '末尾还书',
    pageTitle: '末尾还书调参',
    pageContent:
      '调整末尾还书运动计划参数，并按步骤单步执行验证；保存后立即热生效并持久化到机器人本机服务配置文件。',
    steps: returnSteps,
  },
  midreturn: {
    tab: '中位还书',
    pageTitle: '中位还书调参',
    pageContent:
      '抓取链抓出旧书、晃缝推回并松爪撤出，再从书立取回槽位抓取待还书，插入前重新识别目标书并按其 Y 加插入偏移计算插入点后插入、推回、松爪回抽；保存后立即热生效并持久化到机器人本机服务配置文件。',
    steps: midreturnSteps,
  },
};

const KINDS = Object.keys(FLOWS) as TuningKind[];

export default function TuningPage() {
  const [kind, setKind] = useState<TuningKind>('take');
  const flow = FLOWS[kind];
  return (
    <TuningPanel
      kind={kind}
      pageTitle={flow.pageTitle}
      pageContent={flow.pageContent}
      steps={flow.steps}
      slotValues={[1, 2, 3, 4, 5]}
      tabList={KINDS.map((key) => ({ key, tab: FLOWS[key].tab }))}
      tabActiveKey={kind}
      onTabChange={(key) => setKind(key as TuningKind)}
    />
  );
}

import { PageContainer } from '@ant-design/pro-components';

import { cancelCompleteReturn } from '@/services/libraryRobot';
import TaskProgressCard from '../components/TaskProgressCard';
import CompleteReturnPanel from './CompleteReturnPanel';

export default function QuickReturnPage() {
  return (
    <PageContainer
      title="快速还书"
      content="选择左邻书脊、导航目标点、层高与书立槽位后，机器人先导航到位，再执行中位还书。"
    >
      <CompleteReturnPanel />
      {/* 轮询最新还书任务：页面刷新后也能恢复显示仍在运行的任务。 */}
      <TaskProgressCard
        taskType="return"
        title="当前还书任务"
        cancelTask={cancelCompleteReturn}
      />
    </PageContainer>
  );
}

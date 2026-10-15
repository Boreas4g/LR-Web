import { ControlOutlined } from '@ant-design/icons';
import { PageContainer } from '@ant-design/pro-components';
import { Link } from '@umijs/max';
import { Button } from 'antd';
import { useState } from 'react';

import CompleteTakeoutPanel from './CompleteTakeoutPanel';
import TakeoutProgressCard from './TakeoutProgressCard';

export default function QuickTakeoutPage() {
  // Bumped when a new takeout task is submitted so the progress card refreshes immediately.
  const [progressRefreshKey, setProgressRefreshKey] = useState(0);

  return (
    <PageContainer
      title="快速取书"
      content="选择目标书脊、导航目标点、层高与书立槽位后，机器人先导航到位，再执行识别抓取并放入书立。"
      extra={
        <Link to="/debug/tuning">
          <Button icon={<ControlOutlined />}>流程调参</Button>
        </Link>
      }
    >
      <CompleteTakeoutPanel
        onTaskCreated={() => setProgressRefreshKey((key) => key + 1)}
      />
      <TakeoutProgressCard refreshKey={progressRefreshKey} />
    </PageContainer>
  );
}

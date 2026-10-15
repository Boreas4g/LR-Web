import {
  CheckCircleFilled,
  CloseCircleFilled,
  ExclamationCircleFilled,
} from '@ant-design/icons';
import { PageContainer } from '@ant-design/pro-components';
import { Badge, Card, Space, Tag, Typography } from 'antd';
import type { ReactNode } from 'react';

import type { RobotStatus } from '@/services/libraryRobot/types';

export const statusMeta: Record<RobotStatus, { label: string; color: string }> =
  {
    idle: { label: '待命', color: 'default' },
    navigating: { label: '导航中', color: 'processing' },
    charging: { label: '充电中', color: 'success' },
    manual: { label: '人工接管', color: 'warning' },
    fault: { label: '故障', color: 'error' },
    offline: { label: '离线', color: 'default' },
    busy: { label: '机械臂作业中', color: 'geekblue' },
    mapping: { label: '建图中', color: 'processing' },
  };

export function RobotStatusTag({ status }: { status: RobotStatus }) {
  const meta = statusMeta[status];
  return <Tag color={meta.color}>{meta.label}</Tag>;
}

export function AlertIcon({
  severity,
}: {
  severity: 'critical' | 'warning' | 'info';
}) {
  if (severity === 'critical')
    return <CloseCircleFilled style={{ color: '#cf1322' }} />;
  if (severity === 'warning')
    return <ExclamationCircleFilled style={{ color: '#d48806' }} />;
  return <CheckCircleFilled style={{ color: '#1677ff' }} />;
}

export function PlaceholderPage({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children?: ReactNode;
}) {
  return (
    <PageContainer title={title} content={description}>
      {children || (
        <Card>
          <Space direction="vertical" size={12}>
            <Badge status="processing" text="平台页面骨架已就绪" />
            <Typography.Text type="secondary">
              该页面将在中心控制 API、ROS 2 Gateway
              和权限策略接入后加载实时数据。
            </Typography.Text>
          </Space>
        </Card>
      )}
    </PageContainer>
  );
}

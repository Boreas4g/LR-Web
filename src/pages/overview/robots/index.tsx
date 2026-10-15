import { DeleteOutlined, MoreOutlined, PlusOutlined } from '@ant-design/icons';
import {
  ModalForm,
  PageContainer,
  ProFormText,
  ProTable,
} from '@ant-design/pro-components';
import { Button, Dropdown, Modal, message, Space, Tag, Typography } from 'antd';
import { useEffect, useState } from 'react';

import {
  createRobot as createRobotRequest,
  deleteRobot,
  getRobots,
} from '@/services/libraryRobot';
import type { RobotSummary } from '@/services/libraryRobot/types';
import { RobotStatusTag } from '../../shared';

export default function RobotManagementPage() {
  const [robots, setRobots] = useState<RobotSummary[]>([]);
  const [createOpen, setCreateOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string>();

  async function refreshRobots() {
    setLoading(true);
    try {
      const robotData = await getRobots();
      setRobots(robotData);
      setLoadError(undefined);
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : '未知错误');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refreshRobots();
  }, []);

  async function createRobot(values: {
    id: string;
    name: string;
    gatewayHost: string;
  }) {
    const id = values.id.trim().toUpperCase();
    try {
      await createRobotRequest({ ...values, id });
      // Re-read the source of truth to avoid rendering an in-memory duplicate.
      await refreshRobots();
      message.success(`已登记 ${id}。`);
      setCreateOpen(false);
      return true;
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : '登记机器人失败。',
      );
      return false;
    }
  }

  function deleteRobotPermanently(robot: RobotSummary) {
    Modal.confirm({
      title: `永久删除机器人 ${robot.id}？`,
      content:
        '此操作不可恢复。机器人及其关联的任务、告警和待执行命令将被删除；删除操作本身会保留在审计记录中。',
      okText: '永久删除',
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await deleteRobot(robot.id);
          setRobots((current) =>
            current.filter((item) => item.id !== robot.id),
          );
          message.success(`${robot.id} 已永久删除。`);
        } catch (error) {
          message.error(
            error instanceof Error ? error.message : '删除机器人失败。',
          );
          throw error;
        }
      },
    });
  }

  return (
    <PageContainer
      title="机器人管理"
      content="登记、启停与永久删除机器人。永久删除会清理关联的任务、告警和待执行命令，且不可恢复。"
    >
      <ProTable<RobotSummary>
        rowKey="id"
        dataSource={robots}
        loading={loading}
        locale={{
          emptyText: loadError
            ? `读取机器人失败：${loadError}`
            : '暂无机器人数据',
        }}
        search={false}
        toolBarRender={() => [
          <Button
            key="create"
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => setCreateOpen(true)}
          >
            登记机器人
          </Button>,
        ]}
        tableLayout="fixed"
        columns={[
          {
            title: '机器人',
            width: 190,
            render: (_, item) => (
              <Space direction="vertical" size={0} style={{ minWidth: 0 }}>
                <Typography.Text strong ellipsis={{ tooltip: item.id }}>
                  {item.id}
                </Typography.Text>
                <Typography.Text
                  type="secondary"
                  ellipsis={{ tooltip: item.name }}
                >
                  {item.name}
                </Typography.Text>
              </Space>
            ),
          },
          {
            title: '状态',
            width: 94,
            render: (_, item) => <RobotStatusTag status={item.status} />,
          },
          {
            title: '任务与位置',
            width: 270,
            responsive: ['md'],
            render: (_, item) => (
              <Space direction="vertical" size={0} style={{ minWidth: 0 }}>
                <Typography.Text ellipsis={{ tooltip: item.currentTask }}>
                  {item.currentTask}
                </Typography.Text>
                <Typography.Text
                  type="secondary"
                  ellipsis={{ tooltip: item.location }}
                >
                  {item.location}
                </Typography.Text>
              </Space>
            ),
          },
          {
            title: 'Gateway / 心跳',
            width: 128,
            responsive: ['lg'],
            render: (_, item) => (
              <Tag color={item.status === 'offline' ? 'default' : 'success'}>
                {item.status === 'offline' ? '未连接' : item.updatedAt}
              </Tag>
            ),
          },
          {
            title: '地图版本',
            dataIndex: 'mapVersion',
            width: 126,
            ellipsis: true,
            responsive: ['xl'],
          },
          {
            title: '操作',
            width: 64,
            fixed: 'right',
            render: (_, item) => (
              <Dropdown
                trigger={['click']}
                menu={{
                  items: [
                    {
                      key: 'hardware',
                      label: '硬件配置（待接入）',
                      disabled: true,
                    },
                    { type: 'divider' },
                    {
                      key: 'delete',
                      label: '永久删除',
                      danger: true,
                      icon: <DeleteOutlined />,
                    },
                  ],
                  onClick: ({ key }) => {
                    if (key === 'delete') deleteRobotPermanently(item);
                  },
                }}
              >
                <Button
                  type="text"
                  shape="circle"
                  aria-label={`${item.id} 操作`}
                  icon={<MoreOutlined />}
                />
              </Dropdown>
            ),
          },
        ]}
      />
      <ModalForm
        title="登记机器人"
        open={createOpen}
        modalProps={{
          destroyOnClose: true,
          onCancel: () => setCreateOpen(false),
        }}
        onFinish={createRobot}
      >
        <ProFormText
          name="id"
          label="机器人 ID"
          placeholder="例如：LR-04"
          rules={[
            { required: true },
            { pattern: /^LR-[A-Za-z0-9_-]+$/, message: '格式示例：LR-04' },
          ]}
        />
        <ProFormText
          name="name"
          label="显示名称"
          placeholder="例如：智巡四号"
          rules={[{ required: true }]}
        />
        <ProFormText
          name="gatewayHost"
          label="Gateway 主机地址"
          placeholder="例如：192.168.10.104"
          rules={[{ required: true }]}
        />
      </ModalForm>
    </PageContainer>
  );
}

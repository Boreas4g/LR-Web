import { Button, InputNumber, Space, Typography } from 'antd';

import {
  nearestYawDirection8,
  normalizeDegrees180,
  YAW_DIRECTIONS_8,
  yawArrowCssDegrees,
} from '@/utils/annotation';

/**
 * 可视化朝向录入：8 方位快捷按钮 + 角度数字框 + 实时箭头预览。
 * 角度约定与 ROS 一致：0°=地图 +X 方向（右），逆时针为正。
 */
export default function YawDegreesField({
  value,
  onChange,
}: {
  value: number;
  onChange: (degrees: number) => void;
}) {
  const activeIndex = nearestYawDirection8(value);
  return (
    <Space orientation="vertical" size={8} style={{ width: '100%' }}>
      <Space size={[4, 4]} wrap align="center">
        <span
          role="img"
          aria-label="朝向预览"
          style={{
            color: '#1677ff',
            display: 'inline-block',
            fontSize: 22,
            lineHeight: 1,
            marginRight: 8,
            transform: `rotate(${yawArrowCssDegrees(value)}deg)`,
            transition: 'transform 0.15s ease',
          }}
          title={`当前朝向 ${normalizeDegrees180(value).toFixed(0)}°`}
        >
          ↑
        </span>
        {YAW_DIRECTIONS_8.map((direction, index) => (
          <Button
            key={direction.label}
            size="small"
            type={index === activeIndex ? 'primary' : 'default'}
            onClick={() => onChange(direction.degrees)}
          >
            {direction.label}
            {direction.degrees === 180 ? ' ±180°' : ` ${direction.degrees}°`}
          </Button>
        ))}
      </Space>
      <InputNumber
        suffix="度"
        min={-180}
        max={180}
        step={1}
        style={{ width: '100%' }}
        value={value}
        onChange={(next) =>
          onChange(normalizeDegrees180(typeof next === 'number' ? next : 0))
        }
      />
      <Typography.Text type="secondary">
        0°=地图 +X 方向（右），逆时针为正；可点快捷方位或直接输入角度。
      </Typography.Text>
    </Space>
  );
}

import { Empty, Typography } from 'antd';
import { useEffect, useState } from 'react';

import { getMapPreview } from '@/services/libraryRobot';
import type {
  MapAnnotation,
  MapVersion,
  RobotSummary,
} from '@/services/libraryRobot/types';

const SHELF_COLOR = '#1677ff';
const ANNOTATION_COLOR = '#8c8c8c';
const TARGET_COLOR = '#fa8c16';
const ROBOT_COLOR = '#f5222d';

/** Image pixel of a ROS map pose; browser Y grows downward, ROS map Y upward. */
export function mapPoseToPixel(
  pose: { x: number; y: number },
  map: Pick<MapVersion, 'origin' | 'resolution' | 'height'>,
) {
  return {
    x: (pose.x - map.origin.x) / map.resolution,
    y: map.height - 1 - (pose.y - map.origin.y) / map.resolution,
  };
}

/** Percentage position of an image pixel inside the rendered map box. */
export function pixelToPercent(
  pixel: { x: number; y: number },
  map: Pick<MapVersion, 'width' | 'height'>,
) {
  return {
    left: (pixel.x / map.width) * 100,
    top: (pixel.y / map.height) * 100,
  };
}

/** CSS rotation (degrees, clockwise) for a ROS map yaw: screen up is map +y. */
export function yawToScreenDegrees(yaw: number) {
  // Adding 0 normalizes -0 so yaw 0 compares equal to 0.
  return (-yaw * 180) / Math.PI + 0;
}

function isInsideMap(
  pixel: { x: number; y: number },
  map: Pick<MapVersion, 'width' | 'height'>,
) {
  return (
    pixel.x >= 0 && pixel.x < map.width && pixel.y >= 0 && pixel.y < map.height
  );
}

function MarkerLabel({ color, text }: { color: string; text: string }) {
  return (
    <span
      style={{
        background: `${color}e6`,
        borderRadius: 3,
        color: '#fff',
        fontSize: 11,
        lineHeight: 1.4,
        marginTop: 3,
        padding: '1px 5px',
        whiteSpace: 'nowrap',
      }}
    >
      {text}
    </span>
  );
}

function HeadingMarker({
  yaw,
  color,
  size,
}: {
  yaw: number;
  color: string;
  size: number;
}) {
  return (
    <span
      style={{
        alignItems: 'center',
        background: color,
        border: '2px solid #fff',
        borderRadius: '50%',
        boxShadow: '0 1px 6px #0008',
        display: 'inline-flex',
        height: size,
        justifyContent: 'center',
        width: size,
      }}
    >
      <span
        style={{
          borderBottom: '5px solid transparent',
          borderLeft: '9px solid #fff',
          borderTop: '5px solid transparent',
          display: 'block',
          height: 0,
          transform: `rotate(${yawToScreenDegrees(yaw)}deg)`,
          width: 0,
        }}
      />
    </span>
  );
}

type MapPosePreviewProps = {
  mapId?: string;
  maps: MapVersion[];
  annotations: MapAnnotation[];
  selectedAnnotationId?: string;
  robot?: RobotSummary;
};

/** Static map preview with annotation targets and the live robot pose. */
export default function MapPosePreview({
  mapId,
  maps,
  annotations,
  selectedAnnotationId,
  robot,
}: MapPosePreviewProps) {
  const map = maps.find((item) => item.id === mapId);
  const [previewUrl, setPreviewUrl] = useState<string>();
  const [previewFailed, setPreviewFailed] = useState(false);

  useEffect(() => {
    setPreviewUrl(undefined);
    setPreviewFailed(false);
    if (!mapId) return;
    let cancelled = false;
    let objectUrl: string | undefined;
    getMapPreview(mapId)
      .then((url) => {
        objectUrl = url;
        if (cancelled) {
          URL.revokeObjectURL(url);
          return;
        }
        setPreviewUrl(url);
      })
      .catch(() => {
        if (!cancelled) setPreviewFailed(true);
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [mapId]);

  if (!mapId || !map) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description="暂无可用地图，无法显示位置预览。"
      />
    );
  }

  const robotPixel =
    robot?.pose && robot.mapId === mapId
      ? mapPoseToPixel(robot.pose, map)
      : undefined;
  const robotPosition =
    robotPixel && isInsideMap(robotPixel, map)
      ? pixelToPercent(robotPixel, map)
      : undefined;

  return (
    <div
      style={{
        alignItems: 'center',
        background: '#e8edf3',
        border: '1px solid #d9e0e8',
        borderRadius: 8,
        display: 'flex',
        height: 320,
        justifyContent: 'center',
        overflow: 'hidden',
        padding: 8,
      }}
    >
      {previewUrl ? (
        <div
          style={{
            aspectRatio: `${map.width} / ${map.height}`,
            height: '100%',
            lineHeight: 0,
            maxWidth: '100%',
            position: 'relative',
          }}
        >
          <img
            alt={`${map.name} 地图预览`}
            src={previewUrl}
            style={{
              display: 'block',
              height: '100%',
              imageRendering: 'pixelated',
              objectFit: 'contain',
              width: '100%',
            }}
          />
          {annotations.map((annotation) => {
            const selected = annotation.id === selectedAnnotationId;
            const position = pixelToPercent(
              { x: annotation.pixelX, y: annotation.pixelY },
              map,
            );
            const color = selected
              ? TARGET_COLOR
              : annotation.kind === 'shelf'
                ? SHELF_COLOR
                : ANNOTATION_COLOR;
            return (
              <div
                key={annotation.id}
                style={{
                  alignItems: 'center',
                  display: 'flex',
                  flexDirection: 'column',
                  left: `${position.left}%`,
                  pointerEvents: 'none',
                  position: 'absolute',
                  top: `${position.top}%`,
                  transform: 'translate(-50%, -50%)',
                  zIndex: selected ? 2 : 1,
                }}
              >
                {selected ? (
                  <>
                    <HeadingMarker
                      yaw={annotation.yaw}
                      color={color}
                      size={26}
                    />
                    <MarkerLabel color={color} text="目标" />
                  </>
                ) : (
                  <span
                    style={{
                      background: color,
                      border: '1px solid #fff',
                      borderRadius: '50%',
                      boxShadow: '0 1px 3px #0006',
                      display: 'block',
                      height: 10,
                      width: 10,
                    }}
                  />
                )}
              </div>
            );
          })}
          {robot?.pose && robotPosition ? (
            <div
              style={{
                alignItems: 'center',
                display: 'flex',
                flexDirection: 'column',
                left: `${robotPosition.left}%`,
                pointerEvents: 'none',
                position: 'absolute',
                top: `${robotPosition.top}%`,
                transform: 'translate(-50%, -50%)',
                zIndex: 3,
              }}
            >
              <HeadingMarker
                yaw={robot.pose.yaw}
                color={ROBOT_COLOR}
                size={30}
              />
              <MarkerLabel color={ROBOT_COLOR} text="机器人" />
            </div>
          ) : null}
        </div>
      ) : (
        <Typography.Text type="secondary">
          {previewFailed ? '地图预览加载失败。' : '地图预览加载中…'}
        </Typography.Text>
      )}
    </div>
  );
}

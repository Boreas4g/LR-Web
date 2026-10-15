import { Image, Typography } from 'antd';
import { useEffect, useState } from 'react';

import { getVisionImageUrl } from '@/services/libraryRobot';

/** One auth-protected vision snapshot (spine match / gap overlay). Refetches
 * whenever the url changes, so a version-bumped overwrite shows up on the
 * next task poll. */
export default function VisionImage({
  url,
  label,
}: {
  url: string;
  label: string;
}) {
  const [src, setSrc] = useState<string>();

  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | undefined;
    getVisionImageUrl(url)
      .then((value) => {
        objectUrl = value;
        if (cancelled) {
          URL.revokeObjectURL(value);
          return;
        }
        setSrc(value);
      })
      .catch(() => {
        // 识别图缺失不影响任务信息展示。
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [url]);

  if (!src) return null;
  return (
    <span style={{ display: 'inline-block', textAlign: 'center' }}>
      <Image
        src={src}
        alt={label}
        width={220}
        style={{ display: 'block', borderRadius: 4 }}
      />
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {label}
      </Typography.Text>
    </span>
  );
}

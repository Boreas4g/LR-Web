import {
  JOINT_SCALE,
  type TuningStepDef,
} from '../../circulation/components/TuningPanel';

const JOINTS = ['J1', 'J2', 'J3', 'J4', 'J5', 'J6'];
const XYZ = ['x', 'y', 'z'];
const RPY = ['rx', 'ry', 'rz'];

const durationField = (path: string, label: string) => ({
  path,
  label,
  unit: 's',
  min: 0.5,
  step: 0.5,
  precision: 1,
});

const openSpeedField = {
  path: 'gripper.open_speed_deg_s',
  label: '开爪速度',
  unit: 'deg·s⁻¹',
  min: 0.1,
  step: 1,
  precision: 1,
};

const openDegField = (path: string, label: string) => ({
  path,
  label,
  unit: 'deg',
  min: 0,
  max: 120,
  step: 1,
  precision: 1,
});

const debugFields = [
  {
    path: 'debug_joint',
    label: '调试位关节角',
    unit: 'deg',
    components: JOINTS,
    scale: JOINT_SCALE,
    step: 1,
    precision: 1,
  },
  durationField('debug_duration_s', '运动时长'),
];

export const returnSteps: TuningStepDef[] = [
  {
    title: '调试位',
    fields: [
      {
        path: 'debug_joint',
        label: '调试位关节角',
        unit: 'deg',
        components: JOINTS,
        scale: JOINT_SCALE,
        step: 1,
        precision: 1,
      },
      durationField('debug_duration_s', '运动时长'),
    ],
  },
  {
    title: '张开夹爪',
    hint: '抓取链开始前张开夹爪。',
    fields: [openDegField('pickup_open_deg', '开爪角度'), openSpeedField],
  },
  {
    title: '识别目标书',
    hint: '在当前位姿拍摄书架照片并同步完成识别与选点（需数秒，期间机械臂在调试位等待，不与运动并行以降低整机负载）；识别成功后后续书立取书与插入链基于该目标点执行。使用「准备」的书脊模板。',
    fields: [],
  },
  {
    title: '运动到书槽',
    hint: '使用下方「书立取回槽位」卡片中所选格口的 pre_grasp/grasp 位姿与时长：先 MovePByJ 到 pre_grasp，再 MoveL 到 grasp。依赖「准备」选择的格口。',
    fields: [],
  },
  {
    title: '闭合夹爪',
    hint: '夹住书立上的待还书。',
    fields: [],
  },
  {
    title: '回调试位',
    hint: '先 MoveL 撤到所选格口的 pre_grasp（撤回时长在槽位卡片编辑），再 MoveJ 回调试位。',
    fields: debugFields,
  },
  {
    title: '预插入位',
    hint: '与取书的预抓取位同理：先 MovePByJ 到插入位外侧的预插入位，避免带着书横扫刮碰书立上的其他书。y、z 按「识别目标书」的目标点加偏移固定后，x 由 IK 自动搜索可达的最大值，但不超过「预插入位 X 上限」（默认 -0.20m：过浅的解会让臂翻到别扭构型）。',
    fields: [
      {
        path: 'pre_insert_x_max_m',
        label: '预插入位 X 上限（负值，越贴近 0 越靠外）',
        unit: 'm',
        min: -0.8,
        max: -0.01,
        step: 0.01,
        precision: 2,
      },
      {
        path: 'pre_insert_y_offset_cm',
        label: '预插入 y 偏移（相对插入位）',
        unit: 'cm',
        min: -50,
        max: 50,
        step: 0.5,
        precision: 1,
      },
      {
        path: 'pre_insert_z_offset_cm',
        label: '预插入 z 偏移（相对插入位）',
        unit: 'cm',
        min: -50,
        max: 50,
        step: 0.5,
        precision: 1,
      },
      durationField('pre_insert_duration_s', '运动时长'),
    ],
  },
  {
    title: '运动到插入位',
    hint: 'y = 识别目标书的 Y + 插入 Y 偏移，x/z 复用其位姿，整体再叠加插入偏差；从预插入位短直线 MoveL 到位。',
    fields: [
      {
        path: 'target_y_offset_cm',
        label: '插入 Y 偏移（相对目标书）',
        unit: 'cm',
        min: -50,
        max: 50,
        step: 0.5,
        precision: 1,
      },
      {
        path: 'insert_offset_cm',
        label: '插入偏差',
        unit: 'cm',
        components: XYZ,
        step: 0.5,
        precision: 1,
      },
      {
        path: 'insert_rpy_deg',
        label: '插入姿态',
        unit: 'deg',
        components: RPY,
        step: 1,
        precision: 1,
      },
      durationField('insert_duration_s', '运动时长'),
    ],
  },
  {
    title: '推回书架',
    hint: '基于插入位把书推入架内（x 负值向架内，z 可顺带微调高低；rx/ry/rz 为相对插入姿态的偏移，0 表示保持插入姿态）。',
    fields: [
      {
        path: 'push_x_offset_cm',
        label: '推入偏移 x',
        unit: 'cm',
        step: 0.5,
        precision: 1,
      },
      {
        path: 'push_z_offset_cm',
        label: '推入偏移 z',
        unit: 'cm',
        step: 0.5,
        precision: 1,
      },
      {
        path: 'push_rpy_deg',
        label: '推入姿态偏移',
        unit: 'deg',
        components: RPY,
        step: 1,
        precision: 1,
        fallback: [0, 0, 0],
      },
      durationField('push_duration_s', '运动时长'),
    ],
  },
  {
    title: '张开夹爪',
    hint: '松开书本即可，不是全开。',
    fields: [openDegField('release_open_deg', '松爪开角'), openSpeedField],
  },
  {
    title: '回抽',
    hint: '基于推回位只变 x 把夹爪抽出书架（正值向架外）；姿态固定为 rx 90° / ry 0° / rz 90°，不随插入与推回姿态变化。',
    fields: [
      {
        path: 'retract_x_offset_cm',
        label: '回抽偏移 x',
        unit: 'cm',
        step: 0.5,
        precision: 1,
      },
      durationField('retract_duration_s', '运动时长'),
    ],
  },
  {
    title: '回调试位',
    fields: debugFields,
  },
];

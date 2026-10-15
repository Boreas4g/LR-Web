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

const gripperCloseFields = [
  {
    path: 'gripper.close_deg',
    label: '闭爪角度',
    unit: 'deg',
    min: 0,
    max: 120,
    step: 1,
    precision: 1,
  },
  {
    path: 'gripper.close_speed_deg_s',
    label: '闭爪速度',
    unit: 'deg·s⁻¹',
    min: 0.1,
    step: 1,
    precision: 1,
  },
  {
    path: 'gripper.effort',
    label: '夹持力度',
    min: 0,
    max: 1,
    step: 0.05,
    precision: 2,
  },
  {
    path: 'gripper.kp',
    label: '比例增益 kp',
    min: 0,
    step: 1,
    precision: 1,
  },
  {
    path: 'gripper.kd',
    label: '微分增益 kd',
    min: 0,
    step: 0.5,
    precision: 1,
  },
  {
    path: 'gripper.timeout_s',
    label: '夹爪超时',
    unit: 's',
    min: 1,
    step: 1,
    precision: 0,
  },
  {
    path: 'gripper.carry_effort',
    label: '携持力度',
    min: 0,
    max: 1,
    step: 0.05,
    precision: 2,
  },
  {
    path: 'gripper.carry_lead_deg',
    label: '携持提前角',
    unit: 'deg',
    min: 0,
    step: 0.5,
    precision: 1,
  },
];

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

export const midreturnSteps: TuningStepDef[] = [
  {
    title: '张开夹爪',
    hint: '抓取链开始前张开夹爪。',
    fields: [openDegField('pickup_open_deg', '开爪角度'), openSpeedField],
  },
  {
    title: '推杆归零',
    fields: [
      {
        path: 'rod_home_deg',
        label: '归零到达位置',
        unit: 'deg',
        min: -100,
        max: 90,
        step: 1,
        precision: 1,
      },
      {
        path: 'rod_tolerance_deg',
        label: '推杆到位容差',
        unit: 'deg',
        min: 0.1,
        step: 0.1,
        precision: 1,
      },
      {
        path: 'rod_timeout_s',
        label: '推杆超时',
        unit: 's',
        min: 1,
        step: 1,
        precision: 0,
      },
    ],
  },
  {
    title: '预抓取位',
    hint: '依赖「准备」识别的目标位姿。',
    fields: [
      {
        path: 'pregrasp_offset_cm',
        label: '预抓取偏移',
        unit: 'cm',
        components: XYZ,
        step: 0.5,
        precision: 1,
      },
      {
        path: 'grasp_rpy_deg',
        label: '抓取姿态',
        unit: 'deg',
        components: RPY,
        step: 1,
        precision: 1,
      },
      durationField('pregrasp_duration_s', '运动时长'),
    ],
  },
  {
    title: '逼近目标',
    hint: '依赖「准备」识别的目标位姿。直线逼近段一般只调 X 分量（沿抓取方向的过渡距离）。',
    fields: [
      {
        path: 'approach_offset_cm',
        label: '逼近偏移',
        unit: 'cm',
        components: XYZ,
        step: 0.5,
        precision: 1,
      },
      durationField('approach_duration_s', '运动时长'),
    ],
  },
  {
    title: '推杆拨书',
    fields: [
      {
        path: 'rod_grasp_deg',
        label: '拨书角度',
        unit: 'deg',
        step: 1,
        precision: 1,
      },
      {
        path: 'rod_speed',
        label: '推杆速度',
        min: 1,
        step: 100,
        precision: 0,
      },
      {
        path: 'rod_acceleration',
        label: '推杆加速度',
        min: 1,
        step: 10,
        precision: 0,
      },
    ],
  },
  {
    title: '精抓',
    fields: [
      {
        path: 'fine_grasp_offset_cm',
        label: '精抓偏移',
        unit: 'cm',
        components: XYZ,
        step: 0.5,
        precision: 1,
      },
      {
        path: 'fine_grasp_rpy_deg',
        label: '精抓姿态',
        unit: 'deg',
        components: RPY,
        step: 1,
        precision: 1,
      },
      durationField('fine_grasp_duration_s', '运动时长'),
    ],
  },
  {
    title: '闭合夹爪',
    fields: gripperCloseFields,
  },
  {
    title: '推回',
    hint: '把抓出的书沿 X 轴推回架内（负值向架内）。',
    fields: [
      {
        path: 'push_in_x_offset_cm',
        label: '推入偏移 x',
        unit: 'cm',
        step: 0.5,
        precision: 1,
      },
      durationField('push_in_duration_s', '运动时长'),
    ],
  },
  {
    title: '松开一点夹爪',
    hint: '松开书本即可，不是全开。',
    fields: [openDegField('partial_release_deg', '松爪开角'), openSpeedField],
  },
  {
    title: '再次推回',
    hint: '松爪后再次推回（负值向架内），保证夹爪手指深入书缝。',
    fields: [
      {
        path: 're_push_x_offset_cm',
        label: '再推偏移 x',
        unit: 'cm',
        step: 0.5,
        precision: 1,
      },
      durationField('re_push_duration_s', '运动时长'),
    ],
  },
  {
    title: '推杆归位',
    hint: '再次推回后推杆运动到指定位置，为后续下降/横移清出空间；到位容差与超时复用「推杆归零」卡片。',
    fields: [
      {
        path: 'rod_putback_deg',
        label: '归位到达位置',
        unit: 'deg',
        min: -100,
        max: 90,
        step: 1,
        precision: 1,
      },
    ],
  },
  {
    title: '机械臂下降',
    hint: '相对当前位置沿 Z 轴下降（负值向下）。',
    fields: [
      {
        path: 'lower_z_offset_cm',
        label: '下降偏移 z',
        unit: 'cm',
        step: 0.5,
        precision: 1,
      },
      durationField('lower_duration_s', '运动时长'),
    ],
  },
  {
    title: '完全张开夹爪',
    hint: '下降放稳后完全张开，撑开书缝为还书让出空间。',
    fields: [openDegField('full_open_deg', '全开角度'), openSpeedField],
  },
  {
    title: '闭合夹爪',
    hint: '复用「闭合夹爪」卡片的闭爪参数；重新夹紧书本，横移时带着书走。',
    fields: [],
  },
  {
    title: '机械臂横移',
    hint: '相对当前位置沿 Y 轴横移，带着书对齐书缝。',
    fields: [
      {
        path: 'shift_y_offset_cm',
        label: '横移偏移 y',
        unit: 'cm',
        step: 0.5,
        precision: 1,
      },
      durationField('shift_duration_s', '运动时长'),
    ],
  },
  {
    title: '书本前移',
    hint: '夹爪保持闭合，带着书一起前移（正值向架外）到便于摄像头识别的位置。',
    fields: [
      {
        path: 'carry_forward_x_offset_cm',
        label: '前移偏移 x',
        unit: 'cm',
        step: 0.5,
        precision: 1,
      },
      durationField('carry_forward_duration_s', '运动时长'),
    ],
  },
  {
    title: '松开一点夹爪',
    hint: '横移到位后轻微松开卸力，不是全开。',
    fields: [openDegField('partial_release_deg', '松爪开角'), openSpeedField],
  },
  {
    title: '机械臂前移',
    hint: '相对横移位沿 X 轴前移（正值向架外）。',
    fields: [
      {
        path: 'forward_x_offset_cm',
        label: '前移偏移 x',
        unit: 'cm',
        step: 0.5,
        precision: 1,
      },
      durationField('forward_duration_s', '运动时长'),
    ],
  },
  {
    title: '回调试位（放回后）',
    fields: debugFields,
  },
  {
    title: '张开夹爪（取书槽前）',
    hint: '复用「张开夹爪」卡片的同一个开爪角度。',
    fields: [openDegField('pickup_open_deg', '开爪角度'), openSpeedField],
  },
  {
    title: '运动到书槽',
    hint: '使用下方「书立取回槽位」卡片中所选格口的 pre_grasp/grasp 位姿与时长：先 MovePByJ 到 pre_grasp，再 MoveL 到 grasp。依赖「准备」选择的格口。',
    fields: [],
  },
  {
    title: '闭合夹爪（书槽）',
    hint: '夹住书立上的待还书；复用「闭合夹爪」卡片的闭爪参数。',
    fields: [],
  },
  {
    title: '回调试位（取书后）',
    hint: '先 MoveL 撤到所选格口的 pre_grasp（撤回时长在槽位卡片编辑），再 MoveJ 回调试位，让开相机视野。',
    fields: debugFields,
  },
  {
    title: '识别目标书（插入前）',
    hint: '放回旧书、取回待还书后，用「准备」的同一书脊模板重新识别目标书；放回过程可能让旧书移位，后续预插入位/插入位均基于这次识别的目标点计算。',
    fields: [],
  },
  {
    title: '预插入位',
    hint: '与取书的预抓取位同理：先 MovePByJ 到插入位外侧的预插入位，避免带着书横扫刮碰书立上的其他书。x 为机械臂本体坐标系下的固定值（相对识别点的偏移曾被拖出可达区）；y、z 仍按「识别目标书（插入前）」的目标点加偏移计算。',
    fields: [
      {
        path: 'pre_insert_x_m',
        label: '预插入位 X（本体坐标，负值向架内）',
        unit: 'm',
        min: -1,
        max: 0.5,
        step: 0.01,
        precision: 3,
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
    hint: 'y = 新识别目标书 Y + 插入 Y 偏移，x/z 复用「识别目标书（插入前）」的位姿，整体再叠加插入偏差；从预插入位短直线 MoveL 到位。',
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
    hint: '基于插入位只变 x 把书推入架内（负值向架内）。',
    fields: [
      {
        path: 'push_x_offset_cm',
        label: '推入偏移 x',
        unit: 'cm',
        step: 0.5,
        precision: 1,
      },
      durationField('push_duration_s', '运动时长'),
    ],
  },
  {
    title: '稍微张开夹爪',
    hint: '松开书本即可，不是全开。',
    fields: [openDegField('release_open_deg', '松爪开角'), openSpeedField],
  },
  {
    title: '回抽',
    hint: '基于推回位只变 x 把夹爪抽出书架（正值向架外）。',
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
    title: '回调试位（完成）',
    fields: debugFields,
  },
];

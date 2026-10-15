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

export const takeSteps: TuningStepDef[] = [
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
    title: '打开夹爪',
    fields: [
      {
        path: 'gripper.open_deg',
        label: '开爪角度',
        unit: 'deg',
        min: 0,
        max: 120,
        step: 1,
        precision: 1,
      },
      {
        path: 'gripper.open_speed_deg_s',
        label: '开爪速度',
        unit: 'deg·s⁻¹',
        min: 0.1,
        step: 1,
        precision: 1,
      },
    ],
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
    fields: [
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
    ],
  },
  {
    title: '抓取后撤',
    fields: [
      {
        path: 'post_grasp_offset_cm',
        label: '后撤偏移',
        unit: 'cm',
        components: XYZ,
        step: 0.5,
        precision: 1,
      },
      {
        path: 'post_grasp_rpy_offset_deg',
        label: '后撤姿态偏移',
        unit: 'deg',
        components: RPY,
        step: 1,
        precision: 1,
      },
      durationField('post_grasp_duration_s', '运动时长'),
    ],
  },
  {
    title: '回调试位',
    hint: '复用「调试位」卡片的关节角与运动时长。',
    fields: [],
  },
  { title: '书立预放置', fields: [] },
  { title: '书立放置', fields: [] },
  { title: '下放松爪', fields: [] },
  {
    title: '松开拨杆',
    hint: '复用「推杆归零」卡片的归零到达位置、到位容差与超时。',
    fields: [],
  },
  {
    title: '撤回调试位',
    hint: '复用「调试位」卡片的关节角与运动时长。',
    fields: [],
  },
  {
    title: '待机位',
    fields: [
      {
        path: 'zero_joint',
        label: '待机位关节角',
        unit: 'deg',
        components: JOINTS,
        scale: JOINT_SCALE,
        step: 1,
        precision: 1,
      },
      durationField('zero_duration_s', '运动时长'),
    ],
  },
];

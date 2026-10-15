export default [
  // Landing redirect must precede the '/'-pathed menu groups: react-router
  // breaks path ties by definition order.
  { path: '/', redirect: '/overview/dashboard' },
  // Menu groups use path '/' so their absolute child paths (which span
  // multiple URL prefixes) stay valid under react-router's nesting rules.
  // prerender:false keeps exportStatic from SSR-rendering the group itself.
  // Each group needs a unique `key`: the menu falls back to `path` as the
  // submenu key, and four identical '/' keys would expand/collapse together.
  {
    path: '/',
    key: 'work',
    name: '作业',
    icon: 'inbox',
    prerender: false,
    routes: [
      {
        path: '/overview/dashboard',
        name: '运行总览',
        icon: 'dashboard',
        component: './overview',
      },
      {
        path: '/circulation/takeout',
        name: '快速取书',
        icon: 'thunderbolt',
        component: './circulation/takeout',
      },
      {
        path: '/circulation/return',
        name: '快速还书',
        icon: 'rollback',
        component: './circulation/return',
      },
      {
        path: '/circulation/multi-round',
        name: '多轮取还书',
        icon: 'retweet',
        component: './circulation/multi-round',
      },
      {
        path: '/circulation/books',
        name: '书籍管理',
        icon: 'read',
        component: './circulation/books',
      },
    ],
  },
  {
    path: '/',
    key: 'mapnav',
    name: '地图与导航',
    icon: 'environment',
    prerender: false,
    routes: [
      {
        path: '/library/map',
        name: '地图建模',
        icon: 'environment',
        component: './library/map',
      },
      {
        path: '/library/mapping',
        name: '手动建图',
        icon: 'radarChart',
        component: './library/mapping',
      },
    ],
  },
  {
    path: '/',
    key: 'debug',
    name: '调试中心',
    icon: 'tool',
    prerender: false,
    routes: [
      {
        path: '/circulation/hardware-debug',
        name: '硬件调试',
        icon: 'tool',
        component: './circulation/hardware-debug',
      },
      {
        path: '/debug/tuning',
        name: '流程调参',
        icon: 'control',
        component: './debug/tuning',
      },
      {
        path: '/debug/multi-round',
        name: '多轮取还调试',
        icon: 'retweet',
        component: './debug/multi-round',
      },
      {
        path: '/debug/chassis',
        name: '底盘调试',
        icon: 'tool',
        component: './debug/chassis',
      },
      {
        path: '/debug/arm',
        name: '机械臂调试',
        icon: 'robot',
        component: './debug/arm',
      },
      {
        path: '/debug/stress-test',
        name: '压力测试',
        icon: 'thunderbolt',
        component: './debug/stress-test',
      },
      {
        path: '/circulation/spine-studio',
        name: '视觉调试',
        icon: 'scan',
        component: './circulation/spine-studio',
      },
    ],
  },
  {
    path: '/',
    key: 'system',
    name: '系统',
    icon: 'setting',
    prerender: false,
    routes: [
      {
        path: '/overview/robots',
        name: '机器人管理',
        icon: 'robot',
        component: './overview/robots',
      },
      {
        path: '/dispatch/robots',
        name: '机器人控制',
        icon: 'thunderbolt',
        component: './dispatch/robots',
      },
    ],
  },
  { path: '/circulation/takeout-tuning', redirect: '/debug/tuning' },
  { path: '/circulation/return-tuning', redirect: '/debug/tuning' },
  { path: '/circulation/midreturn-tuning', redirect: '/debug/tuning' },
  { path: '/dispatch/tasks', redirect: '/circulation/takeout' },
  { path: '/overview', redirect: '/overview/dashboard' },
  { path: '/library', redirect: '/library/map' },
  { path: '/dispatch', redirect: '/dispatch/robots' },
  { path: '/circulation', redirect: '/circulation/takeout' },
  { component: './exception/404', path: '/*' },
];

# 项目协作说明

## 项目概况

本项目是基于 Umi Max v4、antd v6 和 ProComponents v3 的 React 企业级应用，同时包含 FastAPI 中心控制 API。

## 常用命令

- `npm start`：启动带模板 Mock 的前端开发服务。
- `npm run dev`：启动前端开发服务并关闭模板 Mock。
- `npm run build`：构建前端产物。
- `npm run lint`：执行 Biome 与 TypeScript 检查。
- `npm run test`：执行前端 Vitest 测试。
- `npx antd lint ./src`：执行 antd 专项检查。
- `npm run openapi`：重新生成 `src/services/` API 客户端。
- `npm run simple`：精简模板页面，不可逆，执行前必须提交代码。
- `npm run biome`：使用 Biome 自动修复格式问题。
- `npm run tsc`：只执行类型检查。

FastAPI 后端命令见 `apps/control-api/README.md`。

## 关键规则

- 不要手动修改 `src/services/ant-design-pro/`；它是自动生成代码，修改契约后执行 `npm run openapi`。
- 仅使用 Biome，不新增 ESLint 或 Prettier；提交前应通过 `npm run lint` 和 `npx antd lint ./src`。
- 编写 antd 组件前执行 `npx antd info <组件名>`，不要凭记忆猜测 API。
- `npm run simple` 不可逆；必须先创建提交或分支。
- 提交信息使用 Conventional Commits。
- TypeScript 使用严格模式，Node.js 版本不低于 22，依赖锁定使用 `package-lock.json`。
- `.umi` 是自动生成目录；开发服务异常时可删除 `src/.umi` 后重启。

## 架构要点

- 配置：`config/config.ts` 是 Umi 配置，`config/routes.ts` 是声明式路由。
- 运行时：`src/app.tsx` 提供布局与初始状态，`src/access.ts` 定义权限，`src/requestErrorConfig.ts` 处理请求错误。
- 状态：`useModel('@@initialState')` 读取当前用户与设置；复杂服务端状态使用 `@tanstack/react-query`。
- 样式优先级：Tailwind CSS v4（布局）→ antd-style / `createStyles`（主题令牌）→ CSS Modules → Less（遗留样式）。
- 前端请求使用 `@umijs/max` 的 `request`；业务接口按页面放置在 `service.ts`。
- 机器人业务前端通过 `src/services/libraryRobot/` 调用 `/api/v1`，由开发代理转发至 FastAPI。
- 中心 API 位于 `apps/control-api/`；不得直接调用 ROS 2、CAN 或串口。硬件控制只在机器人本机 Edge Gateway 完成。

## 编码原则

1. 编码前说明假设与取舍；存在关键歧义时先询问。
2. 只实现当前需求，不添加推测性功能或单次使用的抽象。
3. 只改动必要文件；不顺手重构无关代码。
4. 每项改动都应有可验证的结果，例如测试、类型检查、构建或接口请求。
5. 改动 antd 后执行相应组件检查；改动 FastAPI 后执行 `apps/control-api/tests`。

## 内置 Claude Skills

- `/pro-upgrade`：将 Ant Design Pro 框架部分与最新官方模板比对并升级，同时保留业务代码。
- `/antd`：查询 antd 组件 API、示例、主题令牌，检查废弃用法与协助版本迁移。

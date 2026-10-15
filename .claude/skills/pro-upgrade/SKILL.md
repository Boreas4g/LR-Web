---
name: pro-upgrade
description: >
  当用户要求将 Ant Design Pro 项目升级至最新版本时使用。触发词包括：升级、迁移项目、
  upgrade pro、pro upgrade、migrate pro、update pro、how to upgrade、update to latest。
allowed-tools:
  - Bash(git clone *)
  - Bash(diff *)
  - Bash(npm install)
  - Bash(npm run lint*)
  - Bash(npm run build*)
  - Bash(npm run tsc*)
  - Bash(npx antd *)
  - Bash(rm -rf /tmp/ant-design-pro-upgrade*)
  - Read
  - Edit
  - Write
  - Glob
  - Grep
---

# Ant Design Pro 升级助手

目标是与官方最新模板比对并合并框架改动，同时保留业务代码。

## 前置检查

开始前确认用户已提交或暂存所有修改。若工作区不干净，提醒用户先提交或暂存，并等待确认；升级会改动多个文件。

## 升级流程

### 1. 获取最新模板

```bash
rm -rf /tmp/ant-design-pro-upgrade
git clone --depth=1 https://github.com/ant-design/ant-design-pro.git /tmp/ant-design-pro-upgrade
```

读取模板 `package.json` 确认版本。

### 2. 分类文件

框架文件需要和模板比对：

| 路径 | 说明 |
| --- | --- |
| `package.json` | 只关注依赖、脚本和开发依赖。 |
| `config/config.ts` | 框架配置。 |
| `config/routes.ts` | 只升级结构，保留业务路由。 |
| `config/defaultSettings.ts` | 布局和主题默认值。 |
| `config/proxy.ts` | 升级结构，保留业务代理目标。 |
| `src/app.tsx` | 运行时配置。 |
| `src/access.ts` | 权限定义。 |
| `src/global.tsx`、`src/loading.tsx` | 框架运行文件。 |
| `src/requestErrorConfig.ts`、`src/typings.d.ts` | 请求与全局类型。 |
| `tsconfig.json`、`biome.json`、`.husky/`、`commitlint.config.*` | 工程配置。 |
| `src/services/ant-design-pro/` | 自动生成，禁止手改，升级后执行 `npm run openapi`。 |

业务文件必须保留：`src/pages/**`、`src/components/**`、`src/models/**`、自定义 `src/services/**/*.ts`、`src/locales/**`、`src/utils/**`、`mock/**` 及其他用户新增文件。

### 3. 比对与合并

逐个读取项目与模板的框架文件，识别依赖升级、新增配置、导入路径变化、运行时 API 变化和模板新增文件。

- 对框架文件采用模板结构，但保留明确的业务定制。
- `package.json` 对齐模板依赖版本，保留项目额外依赖。
- `config/routes.ts` 保留业务路由。
- 业务文件只修复框架迁移导致的导入或 API 问题，不重写业务逻辑和样式。
- 不手改自动生成的 API 服务。

### 4. 执行 antd 迁移检查

```bash
npx antd env --format json
npx antd lint ./src --format json --only deprecated
npx antd migrate <当前主版本> <目标主版本> --format json
```

根据检查结果修复问题。

### 5. 安装与验证

```bash
npm install
npm run lint
npm run build
```

处理类型错误、Biome 规则和缺少的同级依赖；需要时执行 `npm run biome` 自动修复。

### 6. 清理与交付

```bash
rm -rf /tmp/ant-design-pro-upgrade
```

按以下分类说明改动：依赖升级、配置调整、代码迁移、新增文件、需要人工检查的内容。提醒用户执行 `npm run openapi`（如使用自动 API 服务）、完整测试并提交修改。

## 原则

- 不硬编码版本，适配任意升级跨度。
- 业务代码优先保留。
- 不确定某处是否为业务定制时先询问，不擅自覆盖。
- 使用 `@ant-design/cli` 获取迁移和 API 信息。
- 始终清理临时模板目录。

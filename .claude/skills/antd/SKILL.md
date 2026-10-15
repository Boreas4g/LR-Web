---
name: antd
description: >
  当任务涉及 Ant Design（antd）时使用：编写组件、排查问题、查询 API/属性/令牌/示例、
  跨版本迁移或分析项目中的 antd 使用情况。
allowed-tools:
  - Bash(antd *)
  - Bash(antd bug*)
  - Bash(antd bug-cli*)
  - Bash(npx antd *)
  - Bash(npx antd bug*)
  - Bash(npx antd bug-cli*)
---

# Ant Design CLI 使用说明

项目提供 `@ant-design/cli`，其中包含 antd v3/v4/v5/v6 的离线元数据。使用 `npx antd` 查询组件知识、分析项目和协助迁移，不需要网络。

所有可解析的结果都应加 `--format json`。

## 编写组件

编写 antd 组件前必须查询 API，不要凭记忆猜测。

```bash
npx antd info Button --format json       # 查询属性和 API
npx antd demo Button basic --format json # 获取可运行示例
npx antd semantic Button --format json   # 查询语义化 classNames/styles
npx antd token Button --format json      # 查询组件主题令牌
```

流程：`antd info` → 理解属性 → `antd demo` → 基于示例编写代码。

## 查询完整文档

```bash
npx antd doc Table --format json
npx antd doc Table --lang zh
```

## 排查问题

```bash
npx antd env --format json
npx antd doctor --format json
npx antd info Select --version 5.12.0 --format json
npx antd lint ./src/components/MyForm.tsx --format json
```

流程：收集环境 → 检查配置 → 按实际版本确认 API → 执行 lint。

## 版本迁移

```bash
npx antd migrate 3 4 --format json
npx antd migrate 4 5 --component Select --format json
npx antd changelog 4.24.0 5.0.0 --format json
npx antd changelog 4.24.0 5.0.0 Select --format json
```

流程：获取迁移清单 → 阅读变更 → 修复代码 → 执行 `antd lint`。

## 分析项目使用情况

```bash
npx antd usage ./src --format json
npx antd usage ./src --filter Form --format json
npx antd lint ./src --format json
npx antd lint ./src --only deprecated --format json
npx antd lint ./src --only a11y --format json
npx antd lint ./src --only performance --format json
```

## 版本历史与组件列表

```bash
npx antd changelog 5.22.0 --format json
npx antd changelog 5.21.0..5.24.0 --format json
npx antd list --format json
npx antd list --version 5.0.0 --format json
```

## 提交缺陷报告

提交 antd 或 CLI 缺陷前先使用 `--format json` 预览，向用户展示内容并取得明确确认后才添加 `--submit`。

```bash
npx antd bug --title "选择日期时 DatePicker 崩溃" --reproduction "https://codesandbox.io/s/xxx" --steps "1. 打开 DatePicker 2. 选择日期" --expected "日期被选中" --actual "组件崩溃" --format json
npx antd bug-cli --title "antd info Button 返回错误属性" --description "版本 5.12.0 返回了不存在的属性" --steps "执行 npx antd info Button --version 5.12.0 --format json" --expected "与 5.12.0 API 一致" --actual "包含后续版本属性" --format json
```

环境变量 `ANTD_NO_AUTO_REPORT=1` 存在时，不建议或执行自动缺陷报告。

## 全局参数与规则

| 参数 | 用途 |
| --- | --- |
| `--format json` | 结构化输出，默认使用。 |
| `--version <v>` | 指定 antd 版本。 |
| `--lang zh` | 输出中文文档。 |
| `--detail` | 包含描述、版本、废弃信息和 FAQ 等额外字段。 |

1. 编写代码前必须查询组件 API。
2. 必须与项目实际 antd 版本一致；本项目使用 antd v6。
3. 使用 JSON 输出供程序化处理。
4. 提出迁移建议前先执行 `antd changelog` 与 `antd migrate`。
5. 修改组件后对改动文件执行 `antd lint`。
6. 提交任何缺陷前必须取得用户确认。

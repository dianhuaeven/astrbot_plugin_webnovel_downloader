# TUI 分支约束

## 分支职责

- `main` 只保留纯 AstrBot 插件版本，用于安装、发布和常规插件修复。
- `tui-dev` 用于本地 TUI 开发，可以包含 `app_tui.py`、TUI 专属依赖和 TUI 使用文档。
- 通用修复应回流到 `main`：凡是修改 `core/`、`runtime.py`、下载流程、书源解析、任务管理等共享逻辑，都应让插件版本受益。

## 模块边界

- `main.py` 是纯 AstrBot 插件入口，不导入 Textual，不处理 TUI 参数，不提供 TUI 模式分支。
- `base.py` / `support.py` 只承载 AstrBot 适配层逻辑。
- `app_tui.py` 是本地 TUI 入口，不导入 AstrBot，也不复用 `base.py` 里的异步封装。
- `core/` 和 `runtime.py` 是插件与 TUI 的共享层。

## 依赖边界

- Textual 等 TUI 依赖不写入正式 `requirements.txt`。
- TUI 依赖放在 `requirements-tui.txt` 或 `requirements-dev.txt`。
- 插件发布分支应保持 AstrBot 安装环境尽量干净。

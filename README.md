# aside_review

iOS A 面审核技能

版本：**1.0.6**。对 iOS 项目执行只读上架风险检查，默认生成中文 A4 PDF。仓库中的技能目录名为 `ios-aside-review`；GitHub 仓库名为 `aside_review`。

## 安装

从 [最新正式版本](https://github.com/LiHiTao/aside_review/releases/latest) 下载 `ios-aside-review.zip`，解压后将整个 `ios-aside-review` 文件夹放入：

- Codex：`~/.codex/skills/`
- 支持相同 SKILL.md 格式的工具：放入该工具的技能目录。

安装副本不要包含 `.git`。在 Codex 中使用 `$ios-aside-review` 并指定 iOS 项目路径即可。复制到桌面的文件夹也可直接安装。

需要 Python 3.10 或更新版本；PDF 需要 ReportLab 和可嵌入中文字体，验证 PDF 还需要 pypdf。Codex 优先使用 bundled Python。其它环境可运行 `python3 -m pip install reportlab pypdf`，并按 SKILL.md 配置中文字体。

## 每次执行前自动更新

技能从固定的公开仓库 `LiHiTao/aside_review` 查询最新正式 Release，并将标签版本与本地 `VERSION` 比较。相同版本继续执行；有更新则下载该发布标签的完整技能，核验版本和文件结构后替换本地安装，再使用新扫描器运行。

```bash
python3 ~/.codex/skills/ios-aside-review/scripts/update_skill.py --check
python3 ~/.codex/skills/ios-aside-review/scripts/audit_ios_a_side.py /path/to/ios-project --format pdf
```

独立检查命令退出码：`0` 表示已是最新，`10` 表示已更新（需重新读取技能说明），`2` 表示失败。扫描器入口也会检查更新，不会在网络失败、版本不符或更新失败时静默执行旧版。正式 Release 以外的分支提交和预发布版本不作为更新源；本地版本更高时不降级。

更新保留旧目录备份以供恢复，失败回滚，不写入被审计项目。Git 仓库和 worktree 用于开发，不自动覆盖；请将发布文件复制为普通技能安装目录使用。

## 审核规则

完整规则见 [SKILL.md](SKILL.md) 与 [规则参考](references/rules.md)。其中：

- 固定检查隐私协议及用户协议：入口必须关联应用内 WKWebView 加载，URL 默认发送 GET 验证响应；不会构建或运行 App。
- 同一档位同时含 `id` 与 `productId` 时只统计真实商品 ID，不需修改项目字段名规避重复计数。
- 档位按商品列表原有价格顺序，从默认 $0.99 起递增判断，不要求名称或商品 ID 包含序号。
- 存在非空应用描述文案即通过描述检查，不要求包含内购用语。
- A 面有效源码必须严格超过 5000 行，排除空行和纯注释；默认直接统计传入工程，`a_side_source_paths` 缺省或 `[]` 均使用根目录，具体口径见 SKILL.md。
- Restore 按实际代码/操作入口检查，协议中“不提供恢复购买”的说明不会因关键词误报。
- LaunchScreen 支持新版 Xcode 文件夹同步，核对对应 target、同步目录与排除名单。
- 静态通过不代表购买、交易验证或真机布局已经通过，也不保证 Apple 审核结果。

## 开发与发布

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

测试使用模拟 GitHub 响应及可注入的协议请求组件，不依赖公网，也不会修改已安装技能。开发者更新 `VERSION` 和发布说明后提交，再创建与版本相同的标签（例如 `v1.0.5`）。发布工作流会运行测试、核对标签与 VERSION，并创建带安装 ZIP 的正式 GitHub Release。只有 Release 发布成功后，使用者才会收到更新。

首个发布标签为 `v1.0.0`。不要复用或移动已经发布的版本标签。

更新 API 依据 [GitHub Releases 文档](https://docs.github.com/en/rest/releases/releases#get-the-latest-release) 和 [仓库 ZIP 下载文档](https://docs.github.com/en/rest/repos/contents#download-a-repository-archive-zip)。

---
name: ios-aside-review
description: 对目录结构不固定的 iOS A 面项目执行只读上架风险审计。检查 StoreKit 内购与 JSON 配置、B 面通知代理冲突、restore 功能、PrivacyInfo.xcprivacy、相机/相册/麦克风/推送/ATT 的 API 使用及 Xcode 配置、敏感词（AI、Dating、赌博、色情等）、第三方 AI 数据共享、App Store 元数据、LaunchScreen.storyboard 和 4–7 个英文字母的 App 名称。用户要求检查、审计、拒审排查、上架前验证，或明确使用 $ios-aside-review 时触发；不得修改目标项目代码或配置。
---

# iOS A 面审核检查

## 目标

使用只读扫描器和规则参考，对任意目录结构的 iOS A 面项目默认仅输出中文 A4 PDF 报告。报告必须列出全部检查项，并区分确定性缺陷、通过证据、静态无法确认事项和警告；不要把沙盒购买、真机弹窗、ATT 实际显示或视觉布局结果伪装成静态通过。

## 版本与执行前更新

初始版本为 `1.0.0`，安装版本以技能根目录 `VERSION` 为准，官方仓库为 `LiHiTao/aside_review`（公开）。每次使用本技能，必须先执行：

```bash
python3 scripts/update_skill.py --check
```

- 退出码 `0`：已确认本地与 GitHub 最新正式 Release 一致，继续审计。
- 退出码 `10`：已更新本地技能。重新读取更新后的 `SKILL.md` 和 `references/rules.md`，再按新版本规则执行。
- 其它非零退出码：检查或更新失败，明确告知原因并停止本次审计；不得静默使用旧版，也不得把失败解释为已是最新。

检查以 GitHub 的正式 Release 为准，不跟随未发布的分支提交或预发布版本。新版本下载完成并核验后才替换本地技能；失败保留原版，成功更新保留旧版备份供恢复。本地版本高于线上版本时不自动降级。Git 开发仓库和 worktree 不自动覆盖，需在独立安装副本运行技能。

扫描器命令本身也执行同样检查；更新后启动新的 Python 进程运行新扫描器，避免继续使用旧代码。更新只影响技能安装目录，不修改被审计的 iOS 项目。公开仓库无需 GitHub 登录即可检查更新。发布及安装方式见仓库 `README.md`。

## 快速使用

从 Skill 目录运行：

```bash
python3 scripts/audit_ios_a_side.py ./path/to/project \
  --format pdf \
  --output-dir /tmp/ios-aside-review-report
```

也可以使用项目级策略覆盖默认规则：

```bash
python3 scripts/audit_ios_a_side.py ./path/to/project \
  --config ./path/to/a-side-policy.json \
  --format pdf
```

未指定 `--output-dir` 时，报告写入系统临时目录。默认拒绝把报告写入被审计项目内部；只有用户明确要求并使用 `--allow-project-output` 时才允许项目内输出。

## 工作流

1. 确认目标目录存在；先读取现有 `AGENTS.md`、项目说明和策略文件，但不要修改它们。
2. 发现 `.xcodeproj`、`.xcworkspace`、`Info.plist`、`.entitlements`、`.storyboard`、`.xcprivacy`、Swift/Objective-C 源码、JSON、字符串文件和商店元数据。不要假设固定目录。
3. 检查非忽略目录中是否存在 `PrivacyInfo.xcprivacy`；A 面默认不需要该文件，发现任意一个即判定为 `FAIL` 并列出全部相对路径。
4. 扫描源码、配置、权限文案、商店元数据和隐私文本中的敏感词；默认覆盖 AI/人工智能、Dating/交友、赌博/博彩、色情/成人内容，发现命中时聚合为一个失败项。
5. 默认忽略 `.git`、`Pods`、`Carthage`、`build`、`DerivedData`、`.build`、`xcuserdata`、`node_modules`、`swiftshield-output` 等生成或第三方目录；策略可追加忽略目录。
6. 先运行 `scripts/audit_ios_a_side.py`，再按 `references/rules.md` 解读证据。扫描器只读目标文件。
7. 输出规则表中的全部检查项。在内部审计结果中为 `FAIL` 保留相对项目根目录的文件路径、行号、实际值、期望值和修复方向；PDF 仅显示完整检查清单与结论，不直接编辑代码。用户明确要求详细证据时再按需提供。
8. 对 `NOT_VERIFIABLE` 给出人工验证步骤，例如 StoreKit 沙盒购买和真机检查 iPad 视觉布局。相机/相册/麦克风/ATT/Push 权限不扩展检查运行时弹窗、ATT 请求时机或自定义授权按钮。
9. 最终回复使用中文，只交付 PDF 报告并汇总四种状态；优先提示 `FAIL`、`WARN` 和 `NOT_VERIFIABLE`，PDF 完整清单不得省略 `PASS`。除非用户明确要求其它格式，不生成或交付 Markdown、JSON 报告。

## 默认策略

```json
{
  "required_prices_usd": ["0.99", "2.99", "9.99", "19.99", "49.99", "99.99"],
  "allow_extra_tiers": true,
  "require_submit_for_review": true,
  "app_name_min_letters": 4,
  "app_name_max_letters": 7,
  "description_max_chars": 55,
  "forbid_restore": true,
  "forbid_notification_delegate": true,
  "forbid_privacy_manifest": true,
  "sensitive_terms": {
    "AI/人工智能": ["AI", "artificial intelligence", "人工智能", "OpenAI", "ChatGPT", "Gemini", "Claude", "LLM"],
    "Dating/交友": ["dating", "dating app", "matchmaking", "约会", "交友", "相亲"],
    "赌博/博彩": ["gambling", "casino", "betting", "poker", "lottery", "赌博", "博彩", "赌场", "下注"],
    "色情/成人内容": ["porn", "pornography", "xxx", "adult content", "sexual", "nude", "色情", "淫秽", "成人内容", "裸体"]
  },
  "ignored_paths": []
}
```

六个价格是默认 B 面基线，额外档位允许存在。不要根据某个项目当前的商品数量改变基线；如 B 面改档位，使用 `--config` 明确覆盖。商品的金币/积分数量不作为检查项，也不在报告中展示；不要要求 product ID、reference_name 或 name 包含金币/积分数量。

档位顺序（IAP-007）根据商品价格判断：默认从 $0.99 起，依次为 $2.99、$9.99、$19.99、$49.99、$99.99，额外档位按价格递增排列。检查商品列表原有顺序，不要求 product ID、reference_name 或 name 包含档位序号；缺少命名序号不能作为无法确认的理由。价格缺失或动态无法解析时保留 `NOT_VERIFIABLE`；明确乱序时为 `FAIL`。自定义价格基线沿用项目策略。

商店元数据购买用途（META-001）仅检查是否存在非空应用描述文案：有应用描述即 `PASS`，不要求文案说明内购方式、积分用途或付费操作，也不以是否存在内购作为触发条件。明确提供但为空的应用描述为 `FAIL`；描述文件缺失或无法识别为 `NOT_VERIFIABLE`。应用描述与内购商品 description、审核备注、README、隐私政策或用户协议应区分，后者不能替代应用描述。

商品 ID 必须有商品语义证据；不得仅因普通 `id` 含点号、价格字样或类似商品的命名就计入内购。通知、任务、路由等业务 ID 不参与商品数量、一致性和大小写统计。Swift 字符串插值或拼接不能截取为商品字面量；确属动态内购但无法解析时保留 `NOT_VERIFIABLE`。具体识别边界见规则参考。

IAP 提交状态默认要求 `submit_for_review: true`。明确为 `false` 或非布尔值时判定 `FAIL`；字段缺失时判定 `NOT_VERIFIABLE`，不得把缺少提交证据自动视为通过。

## 判定约定

- `PASS`：有明确静态证据满足规则。
- `FAIL`：有明确静态证据违反规则，或配置文件确定无效。
- `NOT_VERIFIABLE`：文件缺失、代码动态生成/混淆、只靠运行时才能确认，或没有足够证据。
- `WARN`：发现可疑模式但不足以确定违反规则；必须同时给出人工复核建议。

敏感词检查是确定性文本规则：默认按完整 ASCII 单词或完整中文短语匹配，`main`、`paid` 等不会因为包含字母 `ai` 而误报。源码注释会被忽略；远端配置、服务端下发文案、截图文字和运行时拼接内容不在静态扫描范围内。可通过策略中的 `sensitive_terms` 替换默认词库；设置为空 object 可关闭该项。

权限检查的 API 证据只用于判断该权限是否需要配置：发现相机、相册、麦克风、ATT 或 Push API 使用时，必须能在 Xcode 的 `Info.plist`、`InfoPlist.strings`、Build Settings（`INFOPLIST_KEY_*`）、`.xcconfig` 或 `.entitlements` 中找到对应配置。Push 只检查权限配置：存在值为 `development` / `production` 的有效 `aps-environment` 或启用的 `com.apple.Push` capability 即通过，无需通知用途文案，不因扫描不到 API 而失败。未使用且未配置 Push 时不触发要求；发现 API 但缺少配置时失败。ATT 仅检查 `NSUserTrackingUsageDescription` 配置是否存在以及文案是否合理；配置存在且文案合理即通过，无需发现 ATT 请求调用。ATT-002 只检查文案非空且不是“个性化推荐 / personalized recommendations / personalized content”等通用默认推荐模板；满足即通过。不再要求达到 20 字符或包含追踪、广告、数据对象与用途动作关键词。不得仅因具体用途文案含 personalized ads 就判为默认推荐模板。未使用且未配置 ATT 时不触发要求。不会因为“声明但扫描不到 API”而失败，也不检查文案与产品主题是否匹配、ATT 请求调用、请求时机、系统弹窗、自定义授权按钮或运行时 Push 行为。

`PASS` 不代表 Apple 审核一定通过。尤其是购买成功、隐私同意是否真的阻止发送，以及 iPad 布局，必须在真机或沙盒中复核；权限组本身按本节约定只做静态 API 与 Xcode 配置检查。

## 输出格式

默认 PDF 包含全部顶层检查项。以下 JSON 与 Markdown 约定只适用于用户明确要求这些格式时；内部审计结果仍保留完整证据与子检查。JSON 使用稳定的 `schema_version`、`summary`、`only_failures` 和 `findings` 字段；schema 2.0 默认 `only_failures` 为 `false`，`summary` 固定包含 `PASS`、`FAIL`、`NOT_VERIFIABLE`、`WARN`，`findings` 按规则顺序包含全部检查项。每条 finding 至少包含：

- `id`、`status`、`severity`、`title`
- `expected`、`actual`
- `evidence`：相对项目根目录的路径、行号和摘录；不得输出绝对项目路径
- `manual_check`：需要真机、App Store Connect 或人工确认时提供步骤

Markdown 展示四态汇总、完整检查清单以及每项的期望、实际、证据与人工验证步骤。PDF 为控制篇幅，只展示报告标题和完整检查清单，不展示项目标识、规则版本、统计卡、状态说明、详细检查、聚合子检查、期望、证据或人工复核区块。完整清单仍必须包含规则表中的每个顶层检查项及其状态和结论。所有内购商品配置检查必须聚合为一个“内购项统一检查”结果，不得按商品逐条输出；IAP-001、002、003、005、006、007、008 仍在 JSON 与 Markdown 中作为固定子检查全部展示。PERM-001 聚合相机、相册、麦克风和 Push 的配置检查；PERM-002 检查相机、相册、麦克风用途文案，Push 不参与文案判定。ATT-001 与 ATT-002 独立展示，分别只检查配置和文案。

不传 `--format` 时默认只输出 PDF；`--format pdf` 同样只输出 PDF。仅当用户明确要求时使用 `--format all` 输出三种格式，或 `--format both` 输出 Markdown 与 JSON，保留旧接口兼容。PDF 使用延迟加载的 ReportLab 和可嵌入中文字体；缺少依赖或字体时必须在写入报告前明确失败。优先使用 Codex bundled Python；其他环境可安装 `reportlab`，或通过 `IOS_ASIDE_REVIEW_PDF_FONT` 指定 TTF/TTC 字体。

## PDF 技能与依赖准备

- 生成 PDF 前，先检查当前技能目录是否已安装 `pdf` / `pdf:pdf` 技能；已安装则读取并使用，不重复安装。
- 若未安装，使用可用的 `skill-installer` 工作流查找并安装官方 PDF 技能，然后读取其 `SKILL.md`。用户已授权补装所缺 PDF 技能；不要将技能缺失与 Python 包缺失混为一谈。
- 若安装失败或来源无法确认，明确报告具体原因，不声称已经安装或生成成功。
- 按 PDF 技能要求准备运行时和中文字体，生成后核验 A4 页尺寸、全部检查项，并渲染查看文字与表格是否裁切。所有临时渲染和测试产物必须放在被审计项目之外，结束后清理不再需要的中间文件。

## 规则参考

读取 [references/rules.md](references/rules.md) 了解规则 ID、证据要求和静态限制。需要确定性扫描时优先调用 `scripts/audit_ios_a_side.py`，不要重新发明一次性正则检查。

## 只读边界

不得对目标项目运行格式化器、代码生成、迁移、Xcode 自动修复或写入报告。扫描器只读取文件，并把报告写到项目外临时目录或用户明确指定的外部目录。

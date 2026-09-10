# iOS A 面审核规则

扫描器把规则分成确定性检查和运行时/人工检查，并始终输出本文件列出的全部检查项。金币/积分数量不是本 Skill 的检查项：不要求 product ID、reference_name 或 name 展示数量，也不在报告中输出金币/积分数量。内购报告只展示统一汇总结果，不按商品逐条列出。

状态含义：`PASS` 表示存在明确静态证据，或完整扫描未发现禁止模式；`FAIL` 表示确定违反规则或必要入口缺失；`NOT_VERIFIABLE` 表示证据不足、策略关闭或必须依赖真机/App Store Connect；`WARN` 表示发现可疑模式但证据不足。不得为追求二态结果把 `NOT_VERIFIABLE` 强行改成通过或失败。

## A 面代码规模

| ID | 检查 | 判定 |
|---|---|---|
| CODE-001 | A 面有效代码行数 | 完整有效统计 > `code_line_threshold`（默认 5000）为 PASS，<= 门槛为 FAIL；显式路径无效或读取/解析失败为 NOT_VERIFIABLE。 |

默认统计传入的正常工程根目录，无需确认 A 面归属；`a_side_source_paths` 缺省或为空列表 `[]` 均等效于 `["."]`。非空列表可指定相对根目录的目录/源码文件；无效显式范围不得自动回退。`code_line_excluded_paths` 是相对根目录的精确排除路径（含全部后代）；沿用 `ignored_paths`。路径列表不使用 glob。源码扩展名为 swift/m/mm/h/c/cc/cpp/hpp；配置、资源和文档不计入。

按物理行统计，空白行与纯注释行不计数；代码与行尾注释共存时算一行。括号、声明、import 与多行字符串中的非空内容行均计入。正确识别 Swift 嵌套注释、普通/raw/多行字符串；无法完成词法识别时不得静默使用部分行数。各文件满足总行数 = 空白行 + 纯注释行 + 有效代码行。

默认按目录名忽略（不区分大小写）：`.git`、`Pods`、`Carthage`、`build`、`DerivedData`、`.build`、`xcuserdata`、`node_modules`、`.venv`、`venv`、`swiftshield-output`、`SourcePackages`、`checkouts`、`vendor`、`vendors`、`thirdparty`、`third-party`、`generated`、`generatedsources`、`tests`、`uitests`、`unittests`、`bside`、`b-side`、`b_side`。另外忽略以 `Tests`/`UITests` 结尾的目录，以及文件 stem 以 `Test`/`Tests` 结尾（区分大小写）、以 `test_` 开头或以 `.generated` 结尾（后两者不区分大小写）的源码。不同命名的 B 面、测试、依赖和生成文件通过精确排除路径补充。

默认工程或显式目录没有合格源码时为 0 行失败；同一文件不因重叠路径重复计数。无法按名称识别的 B 面、第三方和生成代码应通过排除路径指定，不根据代码内容猜测归属。PDF 仅显示聚合检查，JSON/Markdown 按需提供文件计数明细。

## 协议端内打开方式

| ID | 检查 | 判定 |
|---|---|---|
| LEGAL-001 | 协议打开方式 | 隐私协议和用户协议的所有可识别入口都必须关联到应用内 WKWebView 的 URL 加载；明确外部打开、Safari view controller、本地 HTML/文件或完整扫描缺少必要入口为 FAIL；动态和不完整证据为 NOT_VERIFIABLE。 |

固定检查隐私协议与用户协议，不扩展到支持页等其它链接。入口、常量/参数和 WKWebView 封装必须属于同一可解析路径；项目中无关的 WebView、Safari、协议字样、注释或示例字符串不会被借用作证据。自定义 openURL 若实际转给 WKWebView 可通过，默认外跳则失败。每个协议的所有入口都参加汇总，不能只选择一个通过入口。缺少 URL 不发送请求；缺少必要协议与动态无法解析的协议分别处理。

UIKit 的 `present(nav)` 可继续追踪 `UINavigationController(rootViewController: pane)` 的根页面，实际 URL 来自该页面加载链；只创建但未呈现的容器不能作证。普通协议页面的 push 沿用原有追踪；嵌套导航控制器或直接 push 导航控制器不能据此判为通过。动态根页面保持需复核，包装 Safari 不改变失败结论。`Terms & Support` / `Terms and Support` 属于用户协议入口名称；普通 `Support` 仅在实际呈现页面的协议标题可关联时归入用户协议，无关帮助页不参与。

仅检查端内 WKWebView 接入，不请求协议 URL、不检测部署或可访问性，不依据 DNS、HTTP 状态、重定向、正文或验证页判定。保留源码 URL/参数及加载链证据；动态路径无法关联时说明静态证据不足。技能 GitHub 自动更新检查独立保留。

PDF 保留 LEGAL-001，移除 LEGAL-002，完整清单共 17 项。JSON/Markdown 不再包含协议请求记录，schema 2.0 不变。测试禁止真实协议请求，验证未部署地址不会影响已有的端内打开证据。

## 内购

| ID | 检查 | 失败条件 |
|---|---|---|
| IAP-001 | B 面价格基线 | 可识别的商品价格缺少 0.99、2.99、9.99、19.99、49.99 或 99.99；额外价格允许。 |
| IAP-002 | 代码/JSON 商品一致 | 两侧都有证据时，product ID 或价格映射不一致。只有一侧可识别时标记 `NOT_VERIFIABLE`。 |
| IAP-003 | 提交审核 | `submit_for_review` 默认要求为布尔值 `true`；明确为 `false` 或非布尔值时失败，字段缺失时标记 `NOT_VERIFIABLE`。 |
| IAP-005 | 描述长度 | description 的 Unicode 字符数超过 55；恰好 55 个字符通过。 |
| IAP-006 | product ID 大小写 | product ID 不是全小写。 |
| IAP-007 | 档位价格顺序 | 商品列表原有价格顺序不是递增排列。默认从 0.99 起，依次为 2.99、9.99、19.99、49.99、99.99；额外档位按价格递增，自定义基线沿用策略。不要求名称或 ID 包含序号，缺少序号不影响通过。缺少可解析价格证据时为 `NOT_VERIFIABLE`。 |
| IAP-008 | JSON 可解析性 | IAP 配置 JSON 无法解析；扫描器可用安全的尾逗号容错继续提取，但仍报告原文件无效。 |

同一记录的显式商品字段优先于通用 `id`：`id: "1"` 与 `productId: "com.huvex.memos1"` 共存时只识别后者，避免七档被计为十四档。优先关系严格限制在同一记录，不借用相邻或嵌套记录的字段，不把注释和字符串当作字段；显式字段动态不可解析时不回退档位 `id`，保留 `NOT_VERIFIABLE`。真实额外商品、大写 ID 和仅使用通用 `id` 的商品语义识别仍须保留。

代码侧优先识别显式商品字段 `productID`/`productId`/`product_id`；通用 `id` 只有具备商品配置匹配或明确的局部商品上下文证据时才作为候选，不得只靠含点号、包含 `coin`/`pack` 等词或文件导入 StoreKit 判定。通知、任务和路由等业务 ID 不纳入商品统计；真正额外或含大写字母的商品仍须检查，不能仅白名单过滤 JSON 中已有的 ID。Swift 插值和拼接表达式不得截取成静态商品 ID，动态内购证据应保留为无法静态确认。JSON 侧识别 `iap_products`、`products`、`in_app_purchases` 等列表中的商品记录。动态拼接、字符串混淆或二进制内配置无法静态证明时不得判定为通过。IAP-001、002、003、005、006、007、008 在报告中合并成一个 `IAP-SUMMARY` finding，并作为固定子检查全部出现。

## A/B 冲突与购买入口

| ID | 检查 | 失败条件 |
|---|---|---|
| AB-001 | 通知代理 | A 面源码实现 `UNUserNotificationCenterDelegate`，或把 `UNUserNotificationCenter.current().delegate` 指向 A 面对象。 |
| IAP-009 | Restore | 检测到实际 `restorePurchases`、`restoreCompletedTransactions`、`AppStore.sync()` 调用/实现，或明确的恢复购买 UI 操作入口。仅协议和帮助说明中的关键词不构成功能证据。 |

协议、帮助文字和嵌入源码的协议字符串（包括明确不提供恢复购买的声明）不得仅因含 `Restore Purchases` 或“恢复购买”判失败。真实调用和按钮仍须检查，不能用同文件或同一行的否定说明屏蔽功能证据。

消费型商品的 `Transaction.updates`、未完成交易处理和 `finish()` 不等于恢复购买，不应误报为 restore。

## 权限、ATT 与 Push

| ID | 检查 | 失败条件 |
|---|---|---|
| PERM-001 | Xcode 权限配置 | 发现相机、相册、麦克风或 Push API 使用，却没有对应 Xcode 配置。相机使用 `NSCameraUsageDescription`，相册使用 `NSPhotoLibraryUsageDescription`/`NSPhotoLibraryAddUsageDescription`，麦克风使用 `NSMicrophoneUsageDescription`，Push 使用 `.entitlements` 中的 `aps-environment` 或已启用的 `com.apple.Push` capability。 |
| PERM-002 | 权限用途文案 | 相机、相册或麦克风配置存在但用途文案为空、少于默认 20 字符，或未说明资源/数据对象和用途动作。Push 不检查用途文案。 |
| ATT-001 | ATT Xcode 配置 | 发现 ATT API 或追踪标识使用，却没有 `NSUserTrackingUsageDescription` 配置。 |
| ATT-002 | ATT 用途文案 | `NSUserTrackingUsageDescription` 存在但为空，或为“个性化推荐 / personalized recommendations / personalized content”等通用默认推荐模板；其它非空文案通过。 |

API 证据只用于判断是否需要配置；配置存在但扫描不到 API 不会失败。相机、相册和麦克风用途文案检查仅验证文本存在、长度和基本的资源/数据对象与用途动作，不判断文案是否契合产品主题。Push 仅检查权限配置：值为 `development` / `production` 的有效 `aps-environment` 或已启用 `com.apple.Push` capability 存在即通过，不检查通知/提醒用途文案。ATT 仅检查配置和文案；配置存在且文案非空、不是通用默认个性化推荐模板即通过；不检查最小长度、追踪/广告关键词或用途动作，不要求存在 ATT 请求调用。具体用途中的 personalized ads 本身不视为默认推荐模板。

PERM-001 按相机、相册、麦克风和 Push 聚合展示；PERM-002 只判定相机、相册和麦克风文案，Push 不参与文案判定；未使用且未配置某权限时，相应子检查以 `PASS` 表示未触发配置要求。ATT-001 与 ATT-002 独立展示。

本组规则不检查 ATT 请求调用、请求时机、系统权限弹窗、清除权限后的流程、自定义授权按钮或运行时 Push 行为；这些不属于本 Skill 的静态判定范围。

## AI 数据共享与商店元数据

| ID | 检查 | 失败条件 |
|---|---|---|
| PRIV-001 | AI 数据共享 | 发现第三方 AI endpoint/SDK 与照片、音频、用户资料等个人数据上传证据，但没有隐私政策中的数据类别、接收方和用途，或没有发送前同意证据。 |
| META-001 | 商店元数据购买用途 | 仅检查非空应用描述是否存在；存在即 `PASS`，明确为空为 `FAIL`，文件缺失或无法识别为 `NOT_VERIFIABLE`。无需包含购买方式、积分用途或付费操作，也不要求项目存在内购。商品 description、审核备注、README 和法律文本不能替代应用描述。 |
| META-002 | 免费/价格声明 | 配置表明 App 下载不是免费，但商店元数据宣称 free/免费；只有截图中的文字无法通过静态文本确认时标记 `NOT_VERIFIABLE`。 |
| META-003 | IAP 提交状态 | IAP 配置中存在商品但没有提交审核，通常由 IAP-003 直接报告。 |

META-003 必须复用 IAP-003 的状态和静态证据，避免同一商品提交状态在报告中出现矛盾结论。年龄分级不属于本 Skill 的检查范围。

## 隐私清单文件

| ID | 检查 | 失败条件 |
|---|---|---|
| PRIV-002 | `PrivacyInfo.xcprivacy` | 非忽略目录中存在任意名为 `PrivacyInfo.xcprivacy` 的文件。A 面默认不需要隐私清单文件；报告列出找到的全部相对路径。 |

默认策略 `forbid_privacy_manifest` 为 `true`。仅当项目策略明确允许隐私清单时将其设为 `false`；此时 `PRIV-002` 仍会输出，但状态为 `NOT_VERIFIABLE` 并说明检查已由策略关闭。

## 敏感词

| ID | 检查 | 失败条件 |
|---|---|---|
| SENSITIVE-001 | 敏感词检查 | 源码、配置、权限文案、商店元数据或隐私文本命中默认敏感词，包括 AI/人工智能、Dating/交友、赌博/博彩、色情/成人内容，或命中项目策略中的自定义词库。 |

敏感词检查按类别聚合为一个 finding，不按词语逐条输出。英文词按完整 ASCII 单词匹配，中文按完整短语匹配；源码注释会被忽略，以降低注释说明造成的误报。默认 `AI` 会匹配独立单词和常见 AI 产品名，但不会匹配 `main`、`paid` 等普通单词。用户可在策略 JSON 中使用 `sensitive_terms` 替换默认类别和词语，使用 `{}` 可关闭敏感词扫描；关闭后该项输出 `NOT_VERIFIABLE`。启用词库且完整扫描无命中时输出 `PASS`。静态扫描不能覆盖远端配置、服务端下发文案、图片截图文字或运行时拼接内容，报告中应提示人工复核这些来源。

## LaunchScreen 与名称

| ID | 检查 | 失败条件 |
|---|---|---|
| IOS-001 | LaunchScreen | target 的 Info.plist 或构建设置没有 `UILaunchStoryboardName`，或对应 storyboard 文件缺失。 |
| IOS-002 | App 名称 | 用户可见 `CFBundleDisplayName`、本地化名称或 App Store 名称中的 ASCII 英文字母数量少于 4 或多于 7。 |

新版 Xcode 文件夹同步工程：解析 `PBXFileSystemSynchronizedRootGroup` 的真实路径、target 的 `fileSystemSynchronizedGroups`、该 target 的构建配置和 `membershipExceptions`。启动配置、磁盘文件、所属 target、同步目录和未排除证据完整即可静态 `PASS`，即使 Resources 的 `files = ()` 为空，且没有逐文件引用。不能仅凭 `objectVersion = 77` 或工程中出现同步目录类型判通过；未关联 target、排除名单命中、路径或配置归属无法解析时保持 `NOT_VERIFIABLE` 并说明原因。同步工程不得回退到文件名字符串匹配，因为排除名单也会包含文件名。静态接入检查不替代实际构建、真机启动与视觉检查。

名称检查默认只统计 `A-Z/a-z`，忽略空格、标点和数字；无法解析构建变量时标记 `NOT_VERIFIABLE`，并列出 `PRODUCT_NAME`/Bundle ID 作为辅助证据。

## 不能静态代替的检查

- StoreKit 沙盒中每个必需 SKU 是否能加载、购买成功、交易验证成功并正确发放。
- iPhone/iPad 上页面是否拥挤、按钮是否可点、内容是否滚动完整。
- App Store Connect 实际价格、截图 OCR 和审核备注。

## 报告格式

- JSON schema 2.0 默认 `only_failures: false`，`summary` 固定包含四种状态，`findings` 按规则顺序输出。
- Markdown 输出四态汇总、完整清单、逐项证据、聚合子检查和人工复核。A4 PDF 只输出报告标题和完整检查清单，不输出项目标识、规则版本、统计卡、状态说明、详细检查、子检查、期望、证据或人工复核区块。
- 默认仅生成 PDF；其它格式只在用户明确要求时使用。`--format pdf` 只生成 `ios-aside-review.pdf`；`--format all` 同时生成 Markdown、JSON 和 PDF；`--format both` 保留 Markdown + JSON。
- PDF 缺少 ReportLab 或可嵌入中文字体时必须在写报告前失败，不留下损坏文件。

# 1.2.2

- 麦克风改为必需配置。缺少 NSMicrophoneUsageDescription 时，PERM-001 配置检查和 PERM-002 用途文案检查均为 FAIL。
- 麦克风已配置时，继续检查用途文案是否非空、达到长度要求，并包含资源对象和用途动作。支持 Info.plist、InfoPlist.strings、Build Settings 和 xcconfig。
- ATT 未使用且未配置时通过；检测到实际使用时要求配置，已配置时检查用途文案。
- 报告沿用 20 项完整清单和 schema 2.0。

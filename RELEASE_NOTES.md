# 1.0.9

- 修复 SwiftUI NavigationLink / Button 使用自定义 Row(title:) 标签时漏掉协议入口的问题，关联外层 destination/action。
- 修复 if let 条件绑定吞掉后续页面内容的问题，保留条件体中的关联加载调用。
- 补充 Swift、Objective-C（.m/.mm）协议入口与 WKWebView 加载回归，保留明确外跳和混合入口失败判定。
- 添加自定义 Row、跨文件协议页面、本地 HTML 加载的脱敏集成样例；保持 17 项清单、schema 2.0 和无协议网络请求。

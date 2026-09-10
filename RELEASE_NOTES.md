# 1.0.6

- 修复协议页面套在 UINavigationController(rootViewController:) 中时无法追踪的误报，继续关联实际呈现的根页面及 WKWebView 加载 URL。
- 识别 Terms & Support / Terms and Support 用户协议入口；普通 Support 不直接视为用户协议，需要实际页面的协议标题关联证据。
- 保留动态根页面、无法解析的路由为需复核；导航容器中的 Safari 或外部打开仍按真实路径判断，不借用未展示的 WebView。
- 新增截图同类 UIKit 跨文件回归，验证协议 URL 能进入联网检查、目标源码保持只读。
- 保持 18 项 PDF 完整清单及 schema 2.0，联网超时、重定向与响应判定规则不变。

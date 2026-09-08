# 1.0.2

- 新增 CODE-001：A 面有效代码行数必须严格超过 5000 行；5000 行失败，5001 行通过。
- 只统计指定 A 面范围的 Swift、Objective-C、C/C++ 源码，排除空行、纯注释、测试、第三方、B 面、生成目录与构建产物。
- 新增 a_side_source_paths、code_line_excluded_paths、code_line_threshold 策略；不明确的范围和不完整统计保留需复核。
- 支持字符串中的注释符号、Swift 嵌套注释及多行/raw 字符串；重叠路径按文件去重。
- PDF 新增行数检查结论，JSON/Markdown 保留按文件计数证据。
- 补充阈值边界、词法识别、排除范围、错误处理和报告集成回归测试。

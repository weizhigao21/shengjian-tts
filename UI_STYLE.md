# 声笺 UI 风格规范

基于 v0.7.12 的桌面界面，用于其他程序复用相同视觉和交互风格。适用于 PyQt、Qt、Web 等技术；下列尺寸为逻辑像素，布局和交互需要另行实现。

## 整体风格

白色工作区、浅灰侧栏、细分隔线、小圆角、紧凑列表。通过字体、间距和线条区分区域，页面以平面布局为主。强调色集中用于焦点、播放进度和少量状态。

## 颜色与字体

| 用途 | 色值 / 尺寸 |
| --- | --- |
| 主背景、内容区 | `#ffffff` |
| 侧栏、轻量悬停 | `#fafafa` |
| 主文字、主按钮背景 | `#18181a` |
| 次要文字 | `#6b6b6e` |
| 辅助标签 | `#85858a` |
| 输入框边框 | `#e5e5e7` |
| 区域分隔线 | `#eeeeee`，1 px |
| 列表选中背景 | `#f1f1f1` |
| 强调色 | `#e0651c` |
| 输入焦点边框 | `#d9ac8c` |
| 就绪状态 | `#83907c` |
| 错误文字 | `#a2462e` |
| 正文 / 辅助文字 | 13 / 12 px |
| 页面标题 / 分区标题 | 23 / 14 px |
| 字体 | Windows 使用 Microsoft YaHei；其他平台使用系统无衬线字体 |

重要内容使用主文字色；浅灰用于标签和次要说明。长路径、版本号和耗时可使用等宽字体。状态同时显示文字，便于识别。

## 布局与尺寸

声笺的工作台布局：

```text
┌──────────┬─────────────────────────┬──────────────┐
│ 品牌     │ 页面标题 / 工具栏       │ 主要操作     │
│ 导航     │                         │ 属性 / 参数  │
│ 最近项目 │ 编辑区 / 内容列表       │ 高级设置     │
│          ├─────────────────────────┴──────────────┤
│ 版本 状态│ 播放 / 当前条目 / 进度 / 导出          │
└──────────┴────────────────────────────────────────┘
```

| 项目 | 声笺基准 |
| --- | --- |
| 默认 / 最小窗口 | 1380×900 / 1080×720 |
| 左侧导航栏 | 208 px，内边距约 12 px |
| 右侧属性栏 | 352 px，左右内边距 18 px |
| 中央内容 | 占用剩余宽度，内边距 18–24 px |
| 底部播放栏 | 72 px，内边距 18×8 px |
| 控件 / 分区间距 | 6–8 / 12–16 px |
| 常规圆角 | 6 px；弹窗 14 px |
| 按钮 / 输入框内边距 | 6×10 / 6×8 px |
| 模型列表 / 音频列表行高 | 50 / 64 px |
| 进度条 | 3 px |
| 图标 | 常规 18 px，细线图标 |

其他程序按功能取用布局：需要编辑时保留中央编辑区，需要配置时显示属性栏，需要播放时加入底栏。内容区随窗口伸缩，空列表只显示简短提示。

## 控件约定

- **主按钮**：深色底、白字，同一区域保留一个明确的主要操作；次要操作使用白底细边框或文字按钮。
- **导航**：图标加文字，选中项使用浅灰背景，设置与其他功能保持在同一导航层级。
- **页签与模式**：2–3 个互斥选项使用紧凑分段按钮，选中项白底细边框。
- **表单**：窄栏使用标签在上、输入在下；短数值可与标签同行。高级参数折叠，控件根据实际能力显示。
- **列表**：名称和描述最多两行，操作靠右；标题、数量、批量操作集中在列表上方。
- **删除**：单行提供图标按钮，多选后启用批量删除；永久删除前显示数量与影响范围。
- **长文本**：路径、状态和名称单行省略，悬停显示完整内容；完整错误使用详情弹窗展示。
- **下载**：名称、大小、细进度条、状态分列。主按钮固定宽度，状态变化只更新内容。
- **底栏**：上一个、播放／暂停、下一个；点击当前条目名称展开列表，再次点击收起。
- **提示与弹窗**：轻量操作结果使用短暂浮动提示；失败提示提供具体原因及下一步，弹窗沿用白底和小圆角控件。

## 交互稳定性

1. 为状态文字和滚动条预留空间。切换模式、显示错误和更新下载进度时，主要按钮保持原位。
2. 定时刷新复用控件，保留焦点、选择、滚动位置和未保存输入。
3. 页面采用整体滚动；数值框、滑条和下拉框中的滚轮用于滚动页面，修改参数通过点击、拖动或输入。
4. 展开列表支持点击外部或按 Esc 关闭，刷新时保持当前展开状态。
5. 异步操作显示进行中状态，完成或失败后恢复可用；输入不完整时在操作附近说明原因。

## PyQt 样式起点

下面的 QSS 可作为其他 Qt 程序的起点。组件通过 `objectName` 和动态属性匹配；布局尺寸、图标、浮层和滚轮行为仍需代码实现。该示例保留核心样式，完整组件状态以本项目 `voice_workbench/ui.py` 为准。

```python
from PyQt6.QtWidgets import QApplication, QPushButton

app = QApplication([])
app.setStyle("Fusion")
app.setStyleSheet("""
QWidget { font-family:"Microsoft YaHei"; font-size:13px; color:#18181a; }
QMainWindow, QWidget#shell { background:white; }
QWidget#sidebar { background:#fafafa; border-right:1px solid #eeeeee; }
QWidget#inspector { background:white; border-left:1px solid #eeeeee; }
QLabel { background:transparent; border:none; }
QLabel[role="title"] { font-size:23px; }
QLabel[role="muted"] { color:#85858a; font-size:12px; }
QPushButton { background:white; border:1px solid #e3e3e5;
    border-radius:6px; padding:6px 10px; min-height:16px; }
QPushButton:hover { background:#f3f3f4; border-color:#d6d6d8; }
QPushButton:pressed { background:#eaeaec; }
QPushButton[kind="primary"] { background:#18181a; color:white; border-color:#18181a; }
QPushButton[kind="primary"]:hover { background:#343436; }
QPushButton:disabled { color:#b0b0b4; background:#f7f7f7; border-color:#eeeeef; }
QPushButton[kind="primary"]:disabled { background:#d6d6d9; border-color:#d6d6d9; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit {
    background:white; border:1px solid #e5e5e7; border-radius:6px; padding:6px 8px; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus,
QDoubleSpinBox:focus, QPlainTextEdit:focus { border-color:#d9ac8c; }
QTableWidget { background:white; border:0; gridline-color:#eeeeee; }
QTableWidget::item:selected { background:#f1f1f1; color:#18181a; }
""")

generate_button = QPushButton("开始生成")
generate_button.setProperty("kind", "primary")
```

本项目使用 `gui.configure_font()` 显式加载系统中文字体，离屏预览也可正常显示。复用到其他框架时，沿用颜色、间距和状态约定，将 QSS 选择器换成对应组件样式。

## 用于其他项目

把本文件交给开发者或代码助手，并提供以下说明：

> 按 UI_STYLE.md 实现紧凑白色工作台。采用浅灰侧栏、细分隔线、6 px 控件圆角、深色主按钮和紧凑列表。根据业务选择中央内容、右侧属性栏与底部操作栏。状态变化保持布局稳定，刷新保留编辑内容，长文本省略并提供完整提示。优先复用现有图标与通用组件。

对应源码：`voice_workbench/ui.py`（样式与组件）、`gui_layout.py`（布局）、`model_controls.py`（表单）、`settings.py`（管理列表）、`library.py`（声音列表与弹窗）。

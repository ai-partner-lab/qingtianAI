# Engine 架构图源文件

本目录的 `engine-overview.excalidraw`、`engine-lifecycle.excalidraw` 是规范源文件，可直接用 Excalidraw 打开编辑。PNG 与 SVG 均由对应场景通过官方 `@excalidraw/excalidraw@0.18.0` 的 `exportToSvg` / `exportToBlob` 导出，没有手写 SVG、Mermaid 或生成图片替代。

- 风格：圆角草图、粉彩白板、Q 版小管家。
- 图例：绿色为已实现，暖黄色为接入方提供，紫色虚线为可选或未集成；具体状态同时写在节点中。
- 字体：场景 `fontFamily:5`，官方 Excalifont 搭配 Xiaolai 中文手写字。先加载字体，再测量元素。
- 交付核查：官方导出字体检查通过；Chrome 实际字形使用 Excalifont 与 Xiaolai SC，均是 custom font，不是系统无衬线替换。SVG 嵌入所需字体子集。
- 修订：直接更新 canonical 场景，再重新官方导出两种图片，检查字体、文字边界和箭头。不要只改 PNG/SVG。

`engine-build-scenes.py` 是初始布局生成器，不是渲染器。它会重建两份布局骨架；**不要在手工修订 canonical 场景后直接运行而覆盖修订**。由生成器起稿时，须经官方 `convertToExcalidrawElements` 规范化为完整原生元素后，再作为 canonical 交付。

本次导出在独立随机 loopback 端口完成，浏览器非本机网络请求被阻止；未操作任何业务或正在运行的控制台服务。导出诊断只含字体信息，不附带私有工作区路径或项目资料。

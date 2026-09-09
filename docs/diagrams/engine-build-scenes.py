"""Generate editable Excalidraw element skeletons; no custom raster/SVG renderer.

Render these scenes with the official Excalidraw exportToSvg/exportToBlob APIs.
The official renderer must load Excalifont and Xiaolai before text measurement.
"""
import json
from pathlib import Path


OUT = Path(__file__).resolve().parent
INK = "#314657"
COLORS = {"implemented": ("#4b8e7a", "#e6f5ea"),
          "adopter": ("#b27a3f", "#fff0d2"),
          "optional": ("#8974ad", "#f0e9fa")}


class Board:
    def __init__(self, name, title, subtitle):
        self.name = name
        self.elements = []
        self.text(250, 43, title, 40)
        self.text(250, 106, subtitle, 25, "#697b85")
        self.mascot()
        for x, kind, title in ((1110, "implemented", "已实现"),
                               (1320, "adopter", "接入方提供"),
                               (1530, "optional", "可选或未集成")):
            self.box(x, 153, 190, 48, kind)
            self.text(x + 95, 163, title, 21, center=True)

    def add(self, kind, **values):
        self.elements.append({"id": self.name + "-" + str(len(self.elements)), "type": kind,
                              "strokeColor": INK, "roughness": 1.5, "seed": 301 + len(self.elements),
                              **values})

    def text(self, x, y, value, size=24, color=INK, center=False):
        self.add("text", x=x, y=y, text=value, fontFamily=5, fontSize=size,
                 textAlign="center" if center else "left", strokeColor=color,
                 lineHeight=1.35)

    def box(self, x, y, width, height, kind="implemented"):
        stroke, fill = COLORS[kind]
        self.add("rectangle", x=x, y=y, width=width, height=height, strokeColor=stroke,
                 backgroundColor=fill, fillStyle="solid", strokeWidth=2,
                 strokeStyle="dashed" if kind == "optional" else "solid", roundness={"type": 3})

    def card(self, x, y, width, height, title, body, kind="implemented", size=24):
        self.box(x, y, width, height, kind)
        self.text(x + 24, y + 20, title, 29)
        self.text(x + 24, y + 68, body, size)

    def arrow(self, points, dashed=False, color="#7291a0"):
        x, y = points[0]
        relatives = [[px - x, py - y] for px, py in points]
        self.add("arrow", x=x, y=y,
                 width=max(px for px, _ in points) - min(px for px, _ in points),
                 height=max(py for _, py in points) - min(py for _, py in points),
                 points=relatives, startArrowhead=None, endArrowhead="arrow",
                 strokeColor=color, strokeWidth=2.4,
                 strokeStyle="dashed" if dashed else "solid")

    def mascot(self):
        self.box(102, 117, 75, 66)
        self.add("ellipse", x=83, y=41, width=111, height=104,
                 backgroundColor="#f3fbf1", fillStyle="solid", strokeWidth=2)
        for x in (116, 158):
            self.add("ellipse", x=x, y=78, width=8, height=13,
                     backgroundColor=INK, fillStyle="solid", roughness=0)
        for x in (102, 168):
            self.add("ellipse", x=x, y=98, width=15, height=8, strokeColor="#e8a6b0",
                     backgroundColor="#f4c4c6", fillStyle="solid", roughness=0.5)
        self.add("line", x=130, y=101, width=20, height=7,
                 points=[[0, 0], [10, 7], [20, 0]], strokeWidth=2)
        self.text(140, 142, "Q", 23, center=True)
        self.add("line", x=102, y=152, width=26, height=22, points=[[0, 0], [-26, -22]], strokeWidth=2)
        self.add("line", x=177, y=150, width=29, height=28, points=[[0, 0], [29, -28]], strokeWidth=2)

    def save(self):
        scene = {"type": "excalidraw", "version": 2, "source": "https://excalidraw.com",
                 "elements": self.elements, "appState": {"viewBackgroundColor": "#fffdf7",
                 "exportBackground": True, "gridSize": None}, "files": {}}
        (OUT / (self.name + ".excalidraw")).write_text(json.dumps(scene, ensure_ascii=False, indent=2) + "\n")


board = Board("engine-overview", "Qingtian Engine / 真实执行引擎",
              "任务能执行，过程有回执，完成要证据")
board.card(60, 245, 330, 175, "收件与看板", "HTTP + CLI + SSE\n文本 / 附件 / 反馈游标")
board.card(460, 245, 460, 175, "任务控制面", "意图 / 角色 / 依赖 / 状态\nmanual 默认，不自动派发")
board.card(1270, 245, 460, 175, "当前项目配置", "Git 仓库 / 开发分支\n权限 / 模型账号 / 测试命令", "adopter")
board.card(60, 490, 330, 210, "可选知识 Provider", "只读 approved 资料\n超时与大小限制\n失败不伪装成功", "optional", 23)
board.card(460, 490, 460, 210, "SQLite 持久账本", "Tasks / Runs / Sessions\nEvents / Evidence / Intake\n去重键 / 检查点 / 反馈游标")
board.card(980, 490, 750, 210, "真正的 Worker 执行", "RunManager 准备独立 worktree\nCodex CLI 接收 stdin，输出 JSONL\n进程 / 会话 / 尝试次数可追踪")
board.card(60, 800, 510, 200, "恢复与人工处理", "实例锁 / lease / 有界重试\nmanual 只核对；auto 显式领取\n暂停与外部等待保留原意图")
board.card(660, 800, 450, 200, "完成证据门", "verified 证据齐备才完成\n退出码 0 不等于 DONE\n缺证据进入 VERIFYING")
board.card(1200, 800, 530, 200, "尚未集成", "独立审计模型 / Dify\n远程队列 / 多节点调度\n不是已有能力或验收结论", "optional")
board.arrow([(390, 330), (460, 330)])
board.arrow([(690, 420), (690, 490)])
board.arrow([(920, 335), (1150, 335), (1150, 490)])
board.text(955, 296, "明确派发", 21)
board.arrow([(1500, 420), (1500, 490)], color="#b27a3f")
board.arrow([(980, 590), (920, 590)])
board.text(925, 549, "事件", 19)
board.arrow([(390, 600), (425, 600), (425, 743), (1350, 743), (1350, 700)], True, "#8974ad")
board.text(910, 713, "只读上下文", 21, "#8974ad")
board.arrow([(535, 700), (535, 800)])
board.arrow([(800, 700), (800, 800)])
board.text(60, 1053, "事实源在账本，不在动画；本地服务不是公网多租户平台。", 28)
board.text(60, 1107, "原生 Excalidraw / Excalifont + Xiaolai / 所有节点可编辑", 21, "#697b85")
board.save()

board = Board("engine-lifecycle", "Qingtian Engine / 执行与回执闭环",
              "Task 是目标，Run 是尝试，Evidence 是完成依据")
board.card(60, 245, 470, 190, "1 明确授权任务", "创建或分析不等于执行\n显式 dispatch 或 implement\nauto 另需明确开启")
board.card(660, 245, 470, 190, "2 路由与准备", "项目 allowlist / 独立 worktree\n依赖与环境检查 / 创建 run\n同任务只保留一个活跃运行")
board.card(1260, 245, 470, 190, "3 真实执行", "Codex CLI / stdin\nJSONL / session / 进程状态\n知识引用不是旧任务授权")
board.card(1260, 555, 470, 210, "4 收回执", "退出码 / 摘要 hash / 结果文件\n首次结果不是已验证证据\nRUN 完成不等于 TASK 完成")
board.card(660, 555, 470, 210, "5 证据校验", "VERIFYING\n按 profile 核对 verified 证据\ncommit / test / artifact 等")
board.card(60, 555, 470, 210, "6 完成与反馈", "DONE 只在证据满足后\n看板 SSE / 报告 / 反馈游标\n不把动画当作成功回执")
board.card(60, 890, 470, 205, "接入方验收", "测试报告与独立复核\n真实 API / 浏览器 / 视觉基线\n不由模型自述替代", "adopter")
board.card(660, 890, 1070, 205, "不足或失败：等待 / 补证 / 恢复", "manual 只核对，不自行重派；auto 可作有界补证与安全恢复\n暂停、依赖、人工与外部等待各自保留原因；耗尽留死信\n数据库 lease 不是所有外部副作用的恰好一次保证")
board.arrow([(530, 335), (660, 335)])
board.arrow([(1130, 335), (1260, 335)])
board.arrow([(1495, 435), (1495, 555)])
board.text(1520, 475, "事件与退出结果", 21)
board.arrow([(1260, 660), (1130, 660)])
board.arrow([(660, 660), (530, 660)])
board.text(553, 616, "齐备", 21)
board.arrow([(960, 765), (960, 890)])
board.text(985, 816, "缺证据或失败", 21)
board.arrow([(1730, 330), (1780, 330), (1780, 982), (1730, 982)])
board.text(1725, 798, "失败", 20)
board.arrow([(530, 984), (598, 984), (598, 823), (770, 823), (770, 765)], False, "#b27a3f")
board.arrow([(1210, 890), (1210, 500), (895, 500), (895, 435)], True)
board.text(934, 467, "显式重试或 auto", 21)
board.text(60, 1150, "只有真实的新任务验证，才能证明接入成功；历史任务与合成 Demo 不能代替。", 27)
board.text(60, 1201, "原生 Excalidraw / Excalifont + Xiaolai / 所有节点可编辑", 21, "#697b85")
board.save()

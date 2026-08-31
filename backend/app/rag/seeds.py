"""手写种子模板知识条目（工单 0009）：应用模板 + 通用技术片段。

每个条目是 (key, title, text)：key 是稳定主键后缀（幂等灌入靠它），
title 与 text 拼接后经 embedding 入库，检索命中时 text 原样注入智能体上下文。
"""

SEED_TEMPLATES: list[dict] = [
    {
        "key": "ledger",
        "title": "记账 / 收支管理应用",
        "text": (
            "适合做家庭记账、日常开支统计。结构：index.html 入口 + style.css 样式 + app.js 逻辑。"
            "要点：表单记录收入/支出（金额、分类、备注、日期），列表展示流水，"
            "用 localStorage 持久化，顶部汇总卡片展示总收入、总支出与结余，"
            "可加按分类的环形图或按月趋势柱状图（ECharts）。"
        ),
    },
    {
        "key": "todo",
        "title": "待办清单 Todo 应用",
        "text": (
            "经典任务管理应用。要点：输入框快速添加任务，列表支持勾选完成、删除与编辑，"
            "过滤器（全部/进行中/已完成），剩余计数，全部清除已完成；"
            "用 localStorage 持久化，空状态给出友好提示。"
        ),
    },
    {
        "key": "pomodoro",
        "title": "番茄钟 / 专注计时器",
        "text": (
            "要点：25 分钟工作 + 5 分钟休息的循环计时，大字号倒计时展示，"
            "开始/暂停/重置按钮，当前轮次与已完成番茄数统计，"
            "结束时可用浏览器通知或提示音；计时状态放内存，统计可用 localStorage 留档。"
        ),
    },
    {
        "key": "weather",
        "title": "天气查询展示页",
        "text": (
            "要点：城市输入与搜索按钮，展示当前温度、天气状况、湿度与风力，"
            "未来几日预报卡片；无后端约束下可用内置模拟数据或公开免费天气 API（fetch），"
            "注意处理加载态与查询无结果的空状态，图标可用 emoji 或 SVG。"
        ),
    },
    {
        "key": "calculator",
        "title": "计算器",
        "text": (
            "要点：数字与运算符按键网格布局，显示屏展示当前输入与结果，"
            "支持连续运算、小数点、清除与退格，注意除零处理与连续按等号的边界；"
            "可用键盘事件增强体验。"
        ),
    },
    {
        "key": "markdown-notes",
        "title": "Markdown 笔记 / 编辑器",
        "text": (
            "要点：左侧编辑区 + 右侧实时预览的双栏布局，用 marked 等 CDN 库解析 Markdown，"
            "笔记列表支持新建、切换、删除与搜索，内容存 localStorage；"
            "注意对用户输入做转义避免 XSS。"
        ),
    },
    {
        "key": "dashboard",
        "title": "数据可视化仪表盘",
        "text": (
            "要点：顶部指标卡片（KPI）+ 多个 ECharts 图表（折线、柱状、饼图）的网格布局，"
            "提供时间范围切换刷新数据；无后端时内置示例数据，"
            "图表在窗口 resize 时调用 chart.resize() 自适应。"
        ),
    },
    {
        "key": "drawing",
        "title": "画板 / 白板涂鸦",
        "text": (
            "要点：canvas 全屏画布，画笔颜色与粗细选择，橡皮、清空、撤销（保存步骤栈），"
            "鼠标与触摸事件都要支持；导出为图片可用 canvas.toDataURL 下载。"
        ),
    },
    {
        "key": "flashcards",
        "title": "记忆卡片 / 单词卡",
        "text": (
            "要点：卡片正面问题、点击翻面显示答案（CSS 3D 翻转动画），"
            "上一张/下一张与随机打乱，记录已掌握/未掌握，进度条展示学习进度；"
            "卡片数据内置或存 localStorage。"
        ),
    },
    {
        "key": "countdown",
        "title": "倒计时 / 纪念日提醒",
        "text": (
            "要点：添加事件（名称 + 日期）列表展示距今天数或倒计时，"
            "每秒刷新大字号的天/时/分/秒，过期事件标记为已过；"
            "日期计算注意时区，用 Date 差值换算。"
        ),
    },
    {
        "key": "landing",
        "title": "产品落地页 / 展示页",
        "text": (
            "要点：首屏大标题 + 行动按钮，特性卡片区、截图展示区与页脚，"
            "用 Tailwind CDN 快速排版，滚动锚点导航，整体注重留白与配色一致性。"
        ),
    },
    {
        "key": "tailwind",
        "title": "通用技巧：Tailwind CDN 布局",
        "text": (
            "在 index.html 头部引入 https://cdn.tailwindcss.com 即可使用原子类；"
            "常用组合：min-h-screen flex 布局、grid grid-cols-* 网格卡片、"
            "rounded-xl shadow 卡片质感、max-w-* mx-auto 内容居中。"
        ),
    },
    {
        "key": "echarts",
        "title": "通用技巧：ECharts 图表接入",
        "text": (
            "用 CDN 引入 echarts 后：const chart = echarts.init(容器DOM)，"
            "chart.setOption({title, tooltip, xAxis, yAxis, series})；"
            "监听 window resize 调 chart.resize()；销毁用 chart.dispose()。"
        ),
    },
    {
        "key": "localstorage",
        "title": "通用技巧：localStorage 持久化",
        "text": (
            "读取：JSON.parse(localStorage.getItem(key) || '[]')，"
            "写入：localStorage.setItem(key, JSON.stringify(data))；"
            "首次加载给默认值，每次增删改后立即写回，注意对象序列化的字段命名一致。"
        ),
    },
]

import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

export type Locale = "en" | "zh-CN";

type Variables = Record<string, string | number>;

interface I18nValue {
  locale: Locale;
  isChinese: boolean;
  setLocale: (locale: Locale) => void;
  t: (key: string, fallback: string, variables?: Variables) => string;
}

const STORAGE_KEY = "minimax-h3-locale";

const zhCN: Record<string, string> = {
  "language.label": "界面语言",
  "common.loading": "加载中…",
  "common.retry": "请重试。",
  "common.all": "全部",
  "common.none": "无",
  "common.remove": "移除",
  "common.clear": "清空",
  "common.cancel": "取消",
  "common.delete": "删除",
  "common.download": "下载",
  "common.close": "关闭",
  "common.save": "保存",
  "common.yes": "是",
  "common.no": "否",
  "app.loadingError": "无法连接服务器，请刷新页面重试。",
  "app.title": "MiniMax H3 生成器",
  "nav.generate": "生成",
  "nav.director": "导演模式",
  "nav.admin": "管理",
  "nav.logout": "退出登录",
  "login.description": "仅限受邀用户使用的 MiniMax H3 ComfyUI 生成平台。",
  "login.checking": "正在检查登录方式…",
  "login.withProvider": "使用 {provider} 登录",
  "login.invite": "没有 {provider} 账号？你需要管理员提供邀请链接；打开链接后即可创建密码账号。",
  "login.password": "已有密码账号？点击这里登录",
  "content.video": "视频",
  "content.image": "图像",
  "content.audio": "音频",
  "content.experimental": "实验性",
  "content.experimentalNotice": "{content}生成为实验功能：它使用极端参数复用视频管线，并非专用模型，结果稳定性低于视频。",
  "mode.t2v": "文字生成视频",
  "mode.i2v": "首帧生成视频",
  "mode.r2v": "参考素材生成",
  "mode.t2i": "文字生成图像",
  "mode.r2i": "参考素材生成",
  "mode.t2a": "文字生成音频",
  "mode.r2a": "参考素材生成",
  "catalog.Draft": "草稿",
  "catalog.Standard": "标准",
  "catalog.Low": "低",
  "catalog.Medium": "中",
  "catalog.High": "高",
  "catalog.Max": "最高",
  "catalog.Sharp": "清晰",
  "catalog.Ultra": "超清",
  "catalog.Fast": "快速",
  "catalog.Rich": "丰富",
  "catalog.Native": "原生",
  "aspect.1:1": "1:1（方形）",
  "aspect.2:3": "2:3（竖版照片）",
  "aspect.3:2": "3:2（横版照片）",
  "aspect.3:4": "3:4（标准竖版）",
  "aspect.4:3": "4:3（标准横版）",
  "aspect.9:16": "9:16（竖屏宽屏）",
  "aspect.16:9": "16:9（宽屏）",
  "aspect.21:9": "21:9（超宽屏）",
  "generate.quality": "质量",
  "generate.model": "模型",
  "generate.modelFp8": "FP8（更快 / 显存占用更低）",
  "generate.modelInt8": "INT8（另一种量化版本）",
  "generate.aspect": "画面比例",
  "generate.resolution": "实际分辨率",
  "generate.resolutionLoading": "正在计算…",
  "generate.resolutionError": "无法计算分辨率",
  "generate.native": "原生上限",
  "generate.roundingNote": "宽高按模型要求对齐到 32 的倍数；此数值与提交任务时完全一致。",
  "generate.capabilityNote": "FP8/INT8 使用相同的已核对 H3 上限：16:9 原生画布最高 1344 × 768；视频训练时长约 5–15 秒。",
  "generate.presetsError": "无法加载质量设置。",
  "generate.length": "时长：{seconds} 秒",
  "generate.renderEstimate": "（预计生成 {duration}）",
  "generate.gpuPool": "GPU 资源池",
  "generate.gpuPoolHint": "只要没有系统级计算进程占用，10 张 GPU 都会参与调度。",
  "generate.modelKeepWarm": "模型确认加载后，空闲保温 {seconds} 秒；没有兼容任务排队时随后自动卸载。",
  "generate.prewarmTime": "预热通常约 10–30 秒；共享磁盘完全冷缓存时可能更久。",
  "generate.prewarm": "预热所选模型",
  "generate.unload": "卸载模型",
  "generate.noModel": "未加载模型",
  "generate.activeModel": "当前任务：{model}（加载中 / 运行中）",
  "generate.gpuError": "暂时无法获取 GPU 状态。",
  "gpu.offline": "离线",
  "gpu.free": "空闲",
  "gpu.starting": "启动中",
  "gpu.standby": "待机（模型未加载）",
  "gpu.ready": "模型就绪",
  "gpu.busy": "生成中",
  "gpu.external": "外部占用",
  "gpu.error": "错误",
  "generate.spectrumAlways": "⚡ 当前部署已始终启用 Spectrum 加速。",
  "generate.spectrum": "⚡ Spectrum 加速（实验性）",
  "generate.spectrumHint": "通过近似跳步提高速度；快速运动或细节可能略有变化，预计时间尚未扣除该加速。",
  "generate.referenceFrames": "参考帧",
  "generate.dropHint": "可点击、拖放或粘贴图片。",
  "generate.firstFrame": "首帧",
  "generate.lastFrame": "末帧（可选）",
  "generate.referenceImages": "参考图片（{count}/{max}）",
  "generate.referenceAudio": "参考音频（{count}/{max}）",
  "generate.referenceVideo": "参考视频（{count}/{max}）",
  "generate.insertToken": "插入引用标记",
  "generate.insertVideoToken": "插入视频标记",
  "generate.insertAudioToken": "插入视频音轨标记",
  "generate.addReferenceImage": "添加参考图片",
  "generate.addReferenceAudio": "添加参考音频",
  "generate.addReferenceVideo": "添加参考视频",
  "generate.tokenHint": "引用标记（如 <Picture 1>）必须保留英文，模型工作流会按该标记识别素材。",
  "generate.referenceAssetHint": "添加素材后，将对应的英文引用标记插入提示词。",
  "generate.referenceVideoHint": "添加视频后，将对应的英文引用标记插入提示词；每个视频会拆成画面帧和独立音轨。",
  "generate.referenceLimits": "Ref2VA 官方上限：最多 9 张图片、3 个参考视频（每个 2–15 秒）和 3 段独立音频。图片仅在需要时缩小，不会放大。",
  "generate.storageNotice": "上传素材和成片会保存在 admin 硬盘，直到你删除任务。GPU 节点的输入、输出和临时文件只在任务执行期间放入系统内存，随后清理。",
  "generate.referenceVideoUnreadable": "无法读取 {name} 的视频时长。",
  "generate.referenceVideoDurationError": "{name} 时长为 {seconds} 秒；参考视频必须为 2–15 秒。",
  "generate.missingReferenceTokens": "当前实际提示词尚未明确引用这些上传素材：{tokens}。素材仍会上传，但使用准确的英文标记更可靠。",
  "generate.prompt": "提示词",
  "generate.promptPlaceholder": "描述你希望生成的内容…",
  "generate.refine": "AI 优化提示词",
  "generate.refining": "正在优化…",
  "generate.chat": "提示词对话",
  "generate.chatOpen": "对话窗口已打开",
  "generate.refineError": "AI 优化失败，请重试。",
  "generate.improvedPrompt": "AI 优化后的版本（实际生成将使用此版本）：",
  "generate.useOriginal": "改用原始提示词",
  "generate.discard": "舍弃优化版本",
  "generate.thisRender": "本次生成：约 {duration}。",
  "generate.ahead": "前方队列：约 {duration}。",
  "generate.emptyQueue": "队列为空。",
  "generate.doneBy": "预计完成时间：{time}。",
  "generate.queueError": "无法加入队列，请重试。",
  "generate.queueVideo": "加入视频队列",
  "generate.queueImage": "加入图像队列",
  "generate.queueAudio": "加入音频队列",
  "generate.queuing": "正在加入队列…",
  "generate.redoRestoring": "正在恢复任务 #{id} 的设置和参考素材…",
  "generate.redoRestored": "已恢复任务 #{id} 的设置和参考素材；请检查后点击加入队列。",
  "job.redoSettings": "使用相同设置",
  "queue.title": "任务队列",
  "queue.notify": "任务完成时通知我",
  "queue.backlog": "队列积压：{duration}",
  "queue.filters": "筛选",
  "queue.filtersActive": "筛选已启用",
  "queue.reset": "重置",
  "queue.quality": "质量",
  "queue.project": "导演项目",
  "queue.noProject": "无项目",
  "queue.favorites": "仅显示收藏",
  "queue.archived": "显示已归档",
  "queue.loadError": "无法加载任务。",
  "queue.empty": "暂无任务；提交一个任务后会显示在这里。",
  "queue.noMatch": "没有符合当前筛选条件的任务。",
  "queue.count": "显示 {shown} / {total} 个",
  "queue.justNow": "刚刚",
  "queue.minutesAgo": "{count} 分钟前",
  "queue.hoursAgo": "{count} 小时前",
  "queue.queued": "排队中",
  "queue.processing": "生成中…",
  "queue.done": "已完成",
  "queue.cancelled": "已取消",
  "queue.failed": "失败",
  "queue.addFavorite": "加入收藏",
  "queue.removeFavorite": "取消收藏",
  "queue.archive": "归档",
  "queue.unarchive": "取消归档",
  "queue.notificationDone": "生成完成",
  "queue.notificationFailed": "生成失败",
  "queue.selectVideos": "选择视频",
  "queue.selectVideo": "选择视频 #{id}",
  "queue.notSelectable": "只能选择已成功完成且尚未加入导演项目的视频。",
  "queue.selectedCount": "已选择：{count}",
  "queue.selectionOrderHint": "视频上的数字就是加入时间线后的初始顺序。",
  "queue.newDirectorProject": "新建导演项目",
  "queue.addToProject": "添加到：{title}",
  "queue.addingVideos": "正在添加…",
  "queue.createFromVideos": "创建项目",
  "queue.appendVideos": "添加到项目",
  "queue.addVideosError": "无法添加所选视频；请确认视频仍然可用后重试。",
  "director.untitled": "未命名项目",
  "director.title": "导演模式",
  "director.description": "将多个片段按顺序合成为长视频，并可选择让相邻场景保持连续。",
  "director.newTitle": "新项目标题…",
  "director.creating": "正在创建…",
  "director.newProject": "新建项目",
  "director.createError": "无法创建项目，请重试。",
  "director.loadError": "无法加载项目。",
  "director.empty": "暂无项目；请先在上方创建项目，或从任务历史多选视频创建。",
  "director.deleteProject": "删除项目？",
  "director.deleteWarning": "“{title}”及其中片段将被移除，此操作无法撤销。",
  "director.deleteVideos": "同时删除该项目片段对应的已生成视频",
  "director.deleteVideosHint": "— 否则视频仍保留在生成历史中，只是不再带有此项目标记；排队中或生成中的视频不会被删除。",
  "director.deleteError": "无法删除项目，请重试。",
  "director.deleting": "正在删除…",
  "director.confirmDelete": "确认删除",
  "director.updated": "更新于 {time}",
  "director.invalidProject": "项目无效。",
  "director.allProjects": "全部项目",
  "director.projectLoadError": "无法加载此项目。",
  "director.clickRename": "点击重命名",
  "director.overarchingPrompt": "项目统一提示词",
  "director.overarchingHint": "每个新生成片段都会使用的世界观、场景与角色约束；修改后相关片段需要重新生成。",
  "director.overarchingPlaceholder": "例如：夜晚的赛博朋克城市，霓虹灯照亮湿润街道…",
  "director.aspectQuality": "画面比例与质量",
  "director.aspectQualityHint": "项目中新生成片段共用这些设置；连续生成需要一致画布，修改后相关片段需要重新生成。",
  "director.generateFromScript": "从脚本生成…",
  "director.checking": "正在检查…",
  "director.checkContinuity": "连续性检查",
  "director.starting": "正在启动…",
  "director.renderDirty": "生成所有待更新片段（{count}）",
  "director.cancelling": "正在取消…",
  "director.cancelAll": "全部取消（{count}）",
  "director.add.t2v": "+ 文字片段",
  "director.add.i2v": "+ 首帧片段",
  "director.add.r2v": "+ 参考片段",
  "director.sharedReferencesMode": "项目已附加共享参考素材，因此新生成片段必须使用参考素材模式。",
  "director.noClips": "暂无片段；可从生成历史多选视频加入，或在时间线末尾新建片段。",
  "director.reviewingPrompts": "正在检查所有片段的提示词…",
  "director.continuityError": "连续性检查失败，请重试。",
  "director.sharedResources": "共享参考素材",
  "director.sharedResourcesHint": "可供所有参考模式片段使用的角色、声音、世界观与风格素材；请在片段提示词中插入类似",
  "director.sharedResourcesHintEnd": "的标记。添加共享素材前，项目中的新生成片段必须全部使用参考模式。",
  "director.convertReferencesHint": "项目中存在非参考模式的新生成片段；添加共享素材前请先转换。已有成片和片段素材都会保留。",
  "director.converting": "正在转换…",
  "director.convertAllReferences": "全部转换为参考模式",
  "director.convertError": "转换失败，请重试。",
  "director.resource.image": "角色设定图 / 参考图片",
  "director.resource.audio": "声音参考",
  "director.resource.video": "参考视频",
  "director.resourceError": "添加参考素材失败，请重试。",
  "director.needsRerender": "需要重新生成",
  "director.notRendered": "尚未生成",
  "director.upToDate": "已是最新",
  "director.includeInExport": "包含在导出中",
  "director.dragToReorder": "拖拽调整顺序",
  "director.moveEarlier": "向前移动",
  "director.moveLater": "向后移动",
  "director.noPrompt": "暂无提示词",
  "director.exportUnavailable": "请至少选择一个已完成且无需重新生成的片段。",
  "director.assembling": "正在合成…",
  "director.exportSelected": "导出所选片段（{count}）",
  "director.downloadExport": "下载合成视频",
  "director.exportError": "合成失败，请重试。",
  "director.exportSelection": "导出选择：{selected}/{total}",
  "director.exportOrderHint": "导出顺序与时间线一致；拖动 ⠿ 可调整顺序。",
  "director.selectAll": "全选",
  "progress.preparing": "准备中…",
  "progress.rendering": "生成中…",
  "progress.finishing": "收尾中…",
  "chat.title": "提示词对话",
  "chat.you": "你：",
  "chat.ai": "AI：",
  "chat.suggested": "建议提示词：",
  "chat.error": "消息发送失败，请重试。",
  "chat.placeholder": "告诉 AI 你想如何修改提示词…",
  "chat.send": "发送",
  "chat.sending": "发送中…",
  "chat.usePrompt": "采用此提示词",
};

function interpolate(template: string, variables?: Variables): string {
  if (!variables) return template;
  return template.replace(/\{(\w+)\}/g, (match, key: string) =>
    Object.prototype.hasOwnProperty.call(variables, key) ? String(variables[key]) : match,
  );
}

function initialLocale(): Locale {
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored === "en" || stored === "zh-CN") return stored;
  return navigator.language.toLowerCase().startsWith("zh") ? "zh-CN" : "en";
}

const I18nContext = createContext<I18nValue | null>(null);

export function LanguageProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(initialLocale);

  useEffect(() => {
    document.documentElement.lang = locale;
    localStorage.setItem(STORAGE_KEY, locale);
  }, [locale]);

  const value = useMemo<I18nValue>(() => {
    const isChinese = locale === "zh-CN";
    return {
      locale,
      isChinese,
      setLocale: setLocaleState,
      t: (key, fallback, variables) =>
        interpolate(isChinese ? (zhCN[key] ?? fallback) : fallback, variables),
    };
  }, [locale]);

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nValue {
  const value = useContext(I18nContext);
  if (!value) throw new Error("useI18n must be used inside LanguageProvider");
  return value;
}

export function LanguageToggle() {
  const { locale, setLocale, t } = useI18n();
  return (
    <label className="language-toggle">
      <span>{t("language.label", "Language")}</span>
      <select value={locale} onChange={(event) => setLocale(event.target.value as Locale)}>
        <option value="zh-CN">中文</option>
        <option value="en">English</option>
      </select>
    </label>
  );
}

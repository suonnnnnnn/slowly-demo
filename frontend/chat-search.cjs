const DEFAULT_BACKEND_URL = "http://127.0.0.1:8000";

function youtubeQueryFromMessages(messages) {
  const latest = [...messages].reverse().find((message) => message.role === "user");
  if (!latest || typeof latest.content !== "string") return null;

  const text = latest.content.replace(/https?:\/\/\S+/gi, " ").replace(/\s+/g, " ").trim();
  if (!text) return null;

  const looksLikeQuestion = /[?？]/.test(text);
  // 触发词分两类：
  // 1) 提问口气：怎么/如何/为什么…、how/what/why…
  // 2) 明确"想做某件事"的口气：我要做 / 我想做 / 番茄炒蛋的做法 / recipe…
  //    这一类既没有问号也不含疑问词，以前整类都漏掉了 ——
  //    「我要做番茄炒蛋」「番茄炒蛋的做法」都搜不出来，看着就像"不检索了"。
  const asksForTutorial = /(怎么|怎样|如何|什么|哪|能不能|可以吗|怎么办|为什么|是否|有没有|想学|教我|帮我|教程|步骤|准备|我要做|我要学|我想做|我想学|想做|要做|要学|做法|食谱|菜谱|配方)/i.test(text)
    || /\b(how (to|do|does|did|can|could|should|about)|what (is|are|should|do|does|about)|why (is|do|does|are)|where (is|do|does|can)|when (is|do|does|should)|which|can (i|you|we)|could (i|you)|should (i|you|we)|teach me|help me|show me|guide me|tutorial|walk me through|steps? (to|for|of)|beginner|recipe|i (want|need|would like|am going) to (make|cook|bake|build|fix|learn|try))\b/i.test(text);
  if (!looksLikeQuestion && !asksForTutorial) return null;
  return text.slice(0, 50);
}

function normalizeVideo(item) {
  if (!item || item.platform !== "youtube" || typeof item.url !== "string") return null;
  if (!/^https:\/\/(www\.)?youtube\.com\/watch\?/i.test(item.url)) return null;
  return {
    platform: "youtube",
    platform_video_id: String(item.platform_video_id || ""),
    url: item.url,
    title: String(item.title || "未命名视频").slice(0, 500),
    uploader: item.uploader ? String(item.uploader).slice(0, 300) : null,
    duration_seconds: Number.isFinite(item.duration_seconds) ? item.duration_seconds : null,
    thumbnail_url: typeof item.thumbnail_url === "string" ? item.thumbnail_url : null,
  };
}

async function searchYoutubeForChat(messages, options = {}) {
  const query = youtubeQueryFromMessages(messages);
  if (!query) return { attempted: false, query: null, cached: false, videos: [], error: null };

  const backendUrl = (options.backendUrl || process.env.VIDEO_BACKEND_URL || DEFAULT_BACKEND_URL).replace(/\/$/, "");
  try {
    const response = await fetch(`${backendUrl}/api/search?q=${encodeURIComponent(query)}`, {
      headers: { Accept: "application/json" },
      signal: AbortSignal.timeout(options.timeoutMs || 12_000),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || `视频检索服务返回 ${response.status}`);
    const videos = Array.isArray(data.items)
      ? data.items.map(normalizeVideo).filter(Boolean).slice(0, 4)
      : [];
    return { attempted: true, query, cached: Boolean(data.cached), videos, error: null };
  } catch (error) {
    console.error("YouTube search failed:", error.message);
    return { attempted: true, query, cached: false, videos: [], error: "暂时无法连接 YouTube 检索服务" };
  }
}

module.exports = { normalizeVideo, searchYoutubeForChat, youtubeQueryFromMessages };

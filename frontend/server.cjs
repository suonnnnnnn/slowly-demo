const http = require("node:http");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { Readable } = require("node:stream");
const { searchYoutubeForChat } = require("./chat-search.cjs");

const root = __dirname;
const envPath = path.join(root, "..", ".env");
if (fs.existsSync(envPath)) {
  for (const line of fs.readFileSync(envPath, "utf8").split("\n")) {
    const at = line.indexOf("=");
    if (at > 0 && !process.env[line.slice(0, at)]) {
      process.env[line.slice(0, at)] = line.slice(at + 1).trim();
    }
  }
}

const videoBackendUrl = (process.env.VIDEO_BACKEND_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
const port = Number.parseInt(process.env.PORT || "8766", 10);
// 默认只绑本机（和原来一样安全）。要在手机上用的话，把 .env 里的 HOST 设成 0.0.0.0，
// 服务会顺带把局域网地址打印出来，手机连同一个 WiFi 直接打开就行。
const host = process.env.HOST || "127.0.0.1";
// 对话模型配置：默认使用 OpenRouter 免费模型，任何 OpenAI 兼容接口都可以用环境变量替换
const modelApiBase = (process.env.MODEL_API_BASE || "https://openrouter.ai/api/v1").replace(/\/+$/, "");
const modelApiKey = process.env.MODEL_API_KEY || process.env.OPENROUTER_API_KEY || "";
const modelName = process.env.MODEL_NAME || "inclusionai/ling-3.0-flash-sante:free";
const sessionCookieName = "slowly_session";
const sessionCookieDays = Number.parseInt(process.env.ANONYMOUS_SESSION_DAYS || "180", 10);

function ensureAnonymousCookie(req, res) {
  const cookies = Object.fromEntries((req.headers.cookie || "").split(";").map((part) => {
    const at = part.indexOf("=");
    return at < 0 ? ["", ""] : [part.slice(0, at).trim(), part.slice(at + 1).trim()];
  }));
  let token = cookies[sessionCookieName];
  if (!/^[A-Za-z0-9_-]{32,128}$/.test(token || "")) {
    token = crypto.randomBytes(32).toString("base64url");
    const secure = String(req.headers["x-forwarded-proto"] || "").split(",", 1)[0] === "https";
    const maxAge = Math.max(1, sessionCookieDays) * 24 * 60 * 60;
    res.setHeader("Set-Cookie", `${sessionCookieName}=${token}; Max-Age=${maxAge}; Path=/; HttpOnly; SameSite=Lax${secure ? "; Secure" : ""}`);
    req.headers.cookie = `${sessionCookieName}=${token}`;
  }
  return token;
}

function requestIsSameOrigin(req) {
  if (!req.headers.origin) return true;
  try {
    const origin = new URL(req.headers.origin);
    return (origin.protocol === "http:" || origin.protocol === "https:")
      && origin.host === req.headers.host;
  } catch {
    return false;
  }
}

async function readBody(req, limit = 30_000) {
  let raw = "";
  for await (const chunk of req) {
    raw += chunk;
    if (raw.length > limit) throw Object.assign(new Error("请求内容过大"), { status: 413 });
  }
  return raw;
}

// 代理层自己的报错也按界面语言走（语言统一在请求 URL 的 ?lang= 上）
const PROXY_STRINGS = {
  backend_timeout: {
    zh: "后端响应超时，这一步可能还在等模型，请稍后重试。",
    en: "The backend timed out — this step may still be waiting on the model. Try again shortly.",
  },
  backend_down: { zh: "暂时无法连接视频下载服务", en: "Can't reach the video service right now" },
  frame_failed: { zh: "暂时无法读取这张代表画面", en: "Couldn't load this step's frame right now" },
  content_failed: { zh: "暂时无法读取已下载的视频", en: "Couldn't load the downloaded video right now" },
};
const proxyText = (res, key) => PROXY_STRINGS[key][langOf(res.req)] || PROXY_STRINGS[key].zh;

async function proxyJson(res, apiPath, options = {}) {
  // 步骤检查要等模型回话，比普通的读任务慢，所以允许调用方放大超时
  const { timeoutMs = 15_000, ...init } = options;
  try {
    const upstream = await fetch(`${videoBackendUrl}${apiPath}`, {
      ...init,
      headers: {
        Accept: "application/json",
        Cookie: res.req.headers.cookie || "",
        "X-Forwarded-Proto": res.req.headers["x-forwarded-proto"] || "http",
        ...(init.headers || {}),
      },
      signal: AbortSignal.timeout(timeoutMs),
    });
    const body = await upstream.text();
    res.writeHead(upstream.status, {
      "Content-Type": upstream.headers.get("content-type") || "application/json; charset=utf-8",
      "Cache-Control": "no-store",
    });
    return res.end(body);
  } catch (error) {
    console.error("Video backend request failed:", error.message);
    return reply(res, error.name === "TimeoutError" ? 504 : 502, {
      error: error.name === "TimeoutError" ? proxyText(res, "backend_timeout") : proxyText(res, "backend_down"),
    });
  }
}

async function proxyBinary(res, apiPath) {
  try {
    const upstream = await fetch(`${videoBackendUrl}${apiPath}`, {
      headers: {
        Cookie: res.req.headers.cookie || "",
        "X-Forwarded-Proto": res.req.headers["x-forwarded-proto"] || "http",
      },
    });
    const headers = { "Cache-Control": "private, max-age=3600" };
    for (const name of ["content-type", "content-length"]) {
      const value = upstream.headers.get(name);
      if (value) headers[name] = value;
    }
    res.writeHead(upstream.status, headers);
    if (!upstream.body) return res.end();
    return Readable.fromWeb(upstream.body).pipe(res);
  } catch (error) {
    console.error("Binary proxy failed:", apiPath, error.message);
    return reply(res, 502, { error: proxyText(res, "frame_failed") });
  }
}

async function proxyVideoContent(req, res, videoId) {
  try {
    const headers = {
      Cookie: req.headers.cookie || "",
      "X-Forwarded-Proto": req.headers["x-forwarded-proto"] || "http",
    };
    if (req.headers.range) headers.Range = req.headers.range;
    const upstream = await fetch(`${videoBackendUrl}/api/videos/${videoId}/content`, { headers });
    const responseHeaders = { "Cache-Control": "private, max-age=3600" };
    for (const name of ["content-type", "content-length", "content-range", "accept-ranges"]) {
      const value = upstream.headers.get(name);
      if (value) responseHeaders[name] = value;
    }
    res.writeHead(upstream.status, responseHeaders);
    if (!upstream.body) return res.end();
    return Readable.fromWeb(upstream.body).pipe(res);
  } catch (error) {
    console.error("Video content proxy failed:", error.message);
    return reply(res, 502, { error: proxyText(res, "content_failed") });
  }
}

// 中文是产品原本的声音；英文版是同一套诚实底线，换一种语言说。
const SYSTEM = {
  zh: `你是“慢慢来”，温暖、简洁的日常教程陪做助手。用简体中文。首先识别用户的具体对象和真实意图，不要一上来按教程详细程度分类。比如“第一次给绿植换盆”，先问是哪种植物，列举绿萝/龟背竹、多肉/仙人掌、兰花等实际品类；允许用户不知道品种。已知品种后再确认换盆原因和当前状态。每轮只问一个最必要的问题，不要一次列举大量问题、分类或答案。等待用户自由输入后继续。回复通常控制在2至4句，用户明确要求步骤时才展开详细内容。不重复问已知信息。澄清充分后按材料、具体操作、完成标准、可能错误和补救提供指导。聊天服务会另外展示 YouTube 实时检索得到的标题和链接；这些结果不是你检索或看过的。你不能观看、解析用户上传的视频或检索结果，绝不能声称看过视频、了解原片内容或编造时间戳。用户要求直接给步骤时可给通用指导，但标明不是视频解析。遇到未知材料/植物时不要作确定性判断。只返回直接给用户看的中文正文，不要输出 JSON、字段名或代码围栏。`,
  en: `You are "Slowly", a warm and concise companion for everyday tutorials. Reply in English. First work out what the user actually wants to make and what they already have — don't jump into a tutorial-mode checklist. For example, on "repotting my plant for the first time", ask which plant it is (pothos, monstera, succulent, orchid...); it's fine if they don't know the species. Once you know, confirm why they're repotting and the current state. Ask only the single most necessary question per turn; don't dump lists, categories or full answers. Wait for the user's own words before moving on. Keep replies to 2-4 sentences; expand into detailed steps only when explicitly asked. Don't repeat what you already know. Once things are clear, guide by materials, concrete actions, done criteria, likely mistakes and fixes. The chat service separately shows titles and links from a live YouTube search; those are results you did NOT search for or watch. You cannot watch or parse uploaded videos or search results, and you must never claim you watched a video, know what's in it, or invent timestamps. If the user asks for steps directly you may give general guidance, but say clearly it is not video parsing. Don't make definitive judgements about unknown materials or plants. Return only the reply text the user should see — no JSON, no field names, no code fences.`,
};

// 界面语言统一用 ?lang=en 带在请求上；没带就是中文。
const langOf = (req) => (/[?&]lang=en(?:&|$)/.test(req && req.url ? req.url : "") ? "en" : "zh");

const CHAT_STRINGS = {
  busy: { zh: "上一条还在回答，请稍等", en: "Still answering the previous one — one moment" },
  bad_input: { zh: "输入格式不正确", en: "That input doesn't look right" },
  bad_message: { zh: "消息格式或长度不正确", en: "Message format or length is invalid" },
  no_key: { zh: "服务端尚未配置密钥", en: "No model key configured on the server yet" },
  rate_limited: { zh: "模型请求受限（429），请稍后再试。", en: "The model rate-limited this request (429) — try again shortly." },
  shared_pool: {
    zh: "免费模型的上游共享额度池正在限流，暂时无法生成回复。请稍后再试；这不是你的输入问题。",
    en: "The free model's upstream shared quota pool is rate-limiting right now, so no reply could be generated. Try again shortly — this is not a problem with your input.",
  },
  provider_status: {
    zh: "模型服务返回 {status}，请检查密钥权限、余额和模型名称。",
    en: "The model service returned {status} — check the key permissions, balance and model name.",
  },
  empty_reply: { zh: "模型未返回内容", en: "The model returned no content" },
  bad_reply_format: { zh: "模型回复格式不完整，请重试。", en: "The model's reply was incomplete — please try again." },
  cross_site: { zh: "不允许跨站请求", en: "Cross-site requests are not allowed" },
};

const chatText = (key, lang) => CHAT_STRINGS[key][lang] || CHAT_STRINGS[key].zh;

function lanAddresses() {
  const found = [];
  for (const list of Object.values(os.networkInterfaces())) {
    for (const net of list || []) {
      if (net.family === "IPv4" && !net.internal) found.push(net.address);
    }
  }
  return found;
}

function parseReply(content) {
  let text = content.trim().replace(/^\x60{3}(?:json)?\s*/i, "").replace(/\s*\x60{3}$/, "");
  for (let i = 0; i < 3; i += 1) {
    try {
      const parsed = JSON.parse(text);
      if (typeof parsed === "string") {
        text = parsed;
        continue;
      }
      if (parsed && typeof parsed.message === "string") {
        text = parsed.message;
        continue;
      }
      return null;
    } catch {
      break;
    }
  }
  if (/^\s*\{/.test(text) || /^\s*\x60{3}/.test(text)) return null;
  return { message: text, options: [] };
}

async function requestModel(messages, lang) {
  const upstream = await fetch(`${modelApiBase}/chat/completions`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${modelApiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model: modelName,
      messages: [{ role: "system", content: SYSTEM[lang] || SYSTEM.zh }, ...messages],
      max_tokens: 900,
      temperature: 0.4,
      stream: false,
    }),
    signal: AbortSignal.timeout(55_000),
  });
  if (!upstream.ok) {
    let providerFailure = {};
    try {
      providerFailure = await upstream.json();
    } catch {}
    const sharedPool = providerFailure.error?.metadata?.limit_source === "upstream_provider_shared_pool";
    console.error("Provider HTTP", upstream.status, "shared_pool", sharedPool);
    const message = upstream.status === 429
      ? (sharedPool ? chatText("shared_pool", lang) : chatText("rate_limited", lang))
      : chatText("provider_status", lang).replace("{status}", upstream.status);
    const error = new Error(message);
    error.status = upstream.status === 429 ? 429 : 502;
    throw error;
  }
  const data = await upstream.json();
  const content = data.choices?.[0]?.message?.content;
  if (!content) throw new Error(chatText("empty_reply", lang));
  const result = parseReply(content);
  if (!result) throw new Error(chatText("bad_reply_format", lang));
  return result;
}

let busy = false;

// PWA 的静态资源（manifest、service worker、图标）也在 dist 下。
// 只放行「最多一层子目录 + 白名单扩展名」，且每段只允许字母数字下划线连字符，
// 所以拼不出 ..，也到不了 dist 之外。
const ASSET_MIME = {
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".webmanifest": "application/manifest+json; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".png": "image/png",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
};
const ASSET_MATCH = /^\/([A-Za-z0-9_-]+\/)?([A-Za-z0-9_-]+\.(?:js|css|webmanifest|json|png|svg|ico))$/;

const reply = (res, status, data) => {
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
  });
  res.end(JSON.stringify(data));
};

http.createServer(async (req, res) => {
  ensureAnonymousCookie(req, res);
  const lang = langOf(req);
  const pathname = req.url.split("?")[0];
  // 部署健康检查走完整链路：Node 能响应、FastAPI 与 SQLite 也都正常才算就绪。
  if (req.method === "GET" && pathname === "/health") {
    return proxyJson(res, "/api/health", { timeoutMs: 10_000 });
  }
  if (req.method === "POST" && pathname === "/api/chat") {
    if (!requestIsSameOrigin(req)) {
      return reply(res, 403, { error: chatText("cross_site", lang) });
    }
    if (busy) return reply(res, 429, { error: chatText("busy", lang) });

    let raw = "";
    try {
      raw = await readBody(req, 60_000);
      const body = JSON.parse(raw);
      if (!Array.isArray(body.messages) || !body.messages.length) throw new Error(chatText("bad_input", lang));
      const messages = body.messages.slice(-12).map((message) => {
        if (!["user", "assistant"].includes(message.role)
          || typeof message.content !== "string"
          || message.content.length > 6000) {
          throw new Error(chatText("bad_message", lang));
        }
        return { role: message.role, content: message.content };
      });
      if (!modelApiKey) return reply(res, 503, { error: chatText("no_key", lang) });

      busy = true;
      const [modelResult, searchResult] = await Promise.all([
        requestModel(messages, lang),
        searchYoutubeForChat(messages, { cookie: req.headers.cookie || "" }),
      ]);
      return reply(res, 200, {
        message: modelResult.message,
        options: Array.isArray(modelResult.options)
          ? modelResult.options.filter((value) => typeof value === "string").slice(0, 4)
          : [],
        videos: searchResult.videos,
        video_search: {
          attempted: searchResult.attempted,
          query: searchResult.query,
          cached: searchResult.cached,
          error: searchResult.error,
        },
      });
    } catch (error) {
      return reply(res, error.status || 502, {
        error: error.name === "TimeoutError"
          ? "回答超时，请重新发送。"
          : (error.message || "请求失败，请稍后重试。"),
      });
    } finally {
      busy = false;
    }
  }

  if (req.method === "POST" && pathname === "/api/videos") {
    if (!requestIsSameOrigin(req)) return reply(res, 403, { error: "不允许跨站请求" });
    try {
      const raw = await readBody(req);
      JSON.parse(raw);
      return proxyJson(res, "/api/videos", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: raw,
      });
    } catch (error) {
      return reply(res, error.status || 400, { error: error.message || "请求格式不正确" });
    }
  }

  // ---- 步骤：真分解（ffmpeg 切点 + 视觉模型看图） + 检查 ----
  const stepsListMatch = pathname.match(/^\/api\/videos\/([A-Za-z0-9-]{1,64})\/steps$/);
  if (req.method === "GET" && stepsListMatch) {
    // query（比如 ?lang=en）要原样带给后端，后端按语言出文案
    const query = req.url.includes("?") ? req.url.slice(req.url.indexOf("?")) : "";
    return proxyJson(res, `/api/videos/${stepsListMatch[1]}/steps${query}`);
  }

  // 重新分解：会清掉已有步骤，所以 force 要原样带给后端，由后端决定要不要拦
  const stepRegenMatch = pathname.match(
    /^\/api\/videos\/([A-Za-z0-9-]{1,64})\/steps\/regenerate$/
  );
  if (req.method === "POST" && stepRegenMatch) {
    if (!requestIsSameOrigin(req)) return reply(res, 403, { error: "不允许跨站请求" });
    const query = req.url.includes("?") ? req.url.slice(req.url.indexOf("?")) : "";
    // 真分解要跑 ffmpeg + 挨段问模型，比普通请求慢得多
    return proxyJson(res, `/api/videos/${stepRegenMatch[1]}/steps/regenerate${query}`, {
      method: "POST",
      timeoutMs: 180_000,
    });
  }

  const stepFrameMatch = pathname.match(
    /^\/api\/videos\/([A-Za-z0-9-]{1,64})\/steps\/([A-Za-z0-9-]{1,64})\/frame$/
  );
  if (req.method === "GET" && stepFrameMatch) {
    return proxyBinary(res, `/api/videos/${stepFrameMatch[1]}/steps/${stepFrameMatch[2]}/frame`);
  }

  const stepActionMatch = pathname.match(
    /^\/api\/videos\/([A-Za-z0-9-]{1,64})\/steps\/([A-Za-z0-9-]{1,64})\/(check|ask)$/
  );
  if (req.method === "POST" && stepActionMatch) {
    if (!requestIsSameOrigin(req)) return reply(res, 403, { error: "不允许跨站请求" });
    const [, videoId, stepId, action] = stepActionMatch;
    // check/ask 的回复语言跟着 ?lang=en 走，query 要带给后端
    const query = req.url.includes("?") ? req.url.slice(req.url.indexOf("?")) : "";
    try {
      // 用户可能带一张照片过来，上限给到 9MB 字符（后端还会再校验一次）
      const raw = await readBody(req, 9_000_000);
      JSON.parse(raw);
      return await proxyJson(res, `/api/videos/${videoId}/steps/${stepId}/${action}${query}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: raw,
        timeoutMs: 60_000,
      });
    } catch (error) {
      return reply(res, error.status || 400, { error: error.message || "请求格式不正确" });
    }
  }

  const stepMatch = pathname.match(/^\/api\/videos\/([A-Za-z0-9-]{1,64})\/steps\/([A-Za-z0-9-]{1,64})$/);
  if (req.method === "PATCH" && stepMatch) {
    if (!requestIsSameOrigin(req)) return reply(res, 403, { error: "不允许跨站请求" });
    const query = req.url.includes("?") ? req.url.slice(req.url.indexOf("?")) : "";
    try {
      const raw = await readBody(req);
      JSON.parse(raw);
      return proxyJson(res, `/api/videos/${stepMatch[1]}/steps/${stepMatch[2]}${query}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: raw,
      });
    } catch (error) {
      return reply(res, error.status || 400, { error: error.message || "请求格式不正确" });
    }
  }

  // 存着慢慢做（列表在首页的 ⋯ 菜单里）
  if (req.method === "GET" && pathname === "/api/saved-tutorials") {
    return proxyJson(res, pathname);
  }

  // 已经完成真实分解的教程素材库
  if (req.method === "GET" && pathname === "/api/library") {
    return proxyJson(res, pathname);
  }

  // 任务列表（可带 ?saved=1 只看存下的教程）
  if (req.method === "GET" && pathname === "/api/videos") {
    return proxyJson(res, req.url);
  }

  const videoMatch = pathname.match(/^\/api\/videos\/([A-Za-z0-9-]{1,64})$/);
  if (req.method === "GET" && videoMatch) {
    return proxyJson(res, `/api/videos/${videoMatch[1]}`);
  }

  // 存下来 / 取消存
  const saveMatch = pathname.match(/^\/api\/videos\/([A-Za-z0-9-]{1,64})\/save$/);
  if (req.method === "POST" && saveMatch) {
    if (!requestIsSameOrigin(req)) return reply(res, 403, { error: "不允许跨站请求" });
    return proxyJson(res, `/api/videos/${saveMatch[1]}/save`, { method: "POST" });
  }
  const contentMatch = pathname.match(/^\/api\/videos\/([A-Za-z0-9-]{1,64})\/content$/);
  if (req.method === "GET" && contentMatch) {
    return proxyVideoContent(req, res, contentMatch[1]);
  }

  const pages = {
    "/": "index.html",
    "/index.html": "index.html",
    "/video.html": "video.html",
    "/vedio.html": "video.html",
    "/steps.html": "steps.html",
    "/library.html": "library.html",
  };
  const libraryAssets = {
    "/assets/library/potato-egg.png": "potato-egg.png",
    "/assets/library/spicy-chicken.png": "spicy-chicken.png",
    "/assets/library/washing-machine.png": "washing-machine.png",
    "/assets/library/tools.png": "tools.png",
    "/assets/library/other.png": "other.png",
  };
  if (req.method === "GET" && libraryAssets[pathname]) {
    res.writeHead(200, {
      "Content-Type": "image/png",
      "Cache-Control": "public, max-age=86400",
      "X-Content-Type-Options": "nosniff",
    });
    return fs.createReadStream(path.join(root, "dist", "assets", "library", libraryAssets[pathname])).pipe(res);
  }
  if (req.method !== "GET" || !pages[pathname]) {
    // 页面之外还有共享的 JS/CSS：只放行白名单扩展名（含 manifest 与 PWA 图标），
    // 每段只允许字母数字下划线连字符，路径拼不进别的目录
    const assetMatch = req.method === "GET" && pathname.match(ASSET_MATCH);
    if (assetMatch) {
      const assetPath = path.join(root, "dist", assetMatch[1] || "", assetMatch[2]);
      const contentType = ASSET_MIME[path.extname(assetPath).toLowerCase()];
      if (contentType && fs.existsSync(assetPath)) {
        res.writeHead(200, {
          "Content-Type": contentType,
          "Cache-Control": "no-store",
          "X-Content-Type-Options": "nosniff",
        });
        return fs.createReadStream(assetPath).pipe(res);
      }
    }
    res.writeHead(404);
    return res.end("Not found");
  }
  res.writeHead(200, {
    "Content-Type": "text/html; charset=utf-8",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
  });
  return fs.createReadStream(path.join(root, "dist", pages[pathname])).pipe(res);
}).listen(port, host, () => {
  if (host === "127.0.0.1") {
    return console.log(`Local demo: http://127.0.0.1:${port}`);
  }
  // 绑到 0.0.0.0 时把局域网地址列出来，手机连同一个 WiFi 直接扫码/输入就能用
  console.log(`Local demo: http://127.0.0.1:${port}`);
  for (const address of lanAddresses()) {
    console.log(`  on your phone: http://${address}:${port}`);
  }
});

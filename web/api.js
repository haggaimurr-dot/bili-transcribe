// fetch / EventSource 封装
const { reactive } = Vue;

export const toasts = reactive([]);
let toastId = 0;

export function toast(message, { hint = "", error = false, ms = 4000 } = {}) {
  const id = ++toastId;
  toasts.push({ id, message, hint, error });
  setTimeout(() => {
    const i = toasts.findIndex((t) => t.id === id);
    if (i >= 0) toasts.splice(i, 1);
  }, ms);
}

export class ApiError extends Error {
  constructor(err) {
    super(err.message);
    this.code = err.code;
    this.hint = err.hint || "";
  }
}

export async function api(path, { method = "GET", body, form, quiet = false } = {}) {
  const opts = { method, headers: {} };
  if (form) opts.body = form;
  else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch("/api" + path, opts);
  } catch (e) {
    const err = new ApiError({ code: "NETWORK", message: "无法连接本地服务", hint: "服务是否已关闭？" });
    if (!quiet) toast(err.message, { hint: err.hint, error: true });
    throw err;
  }
  const ct = res.headers.get("content-type") || "";
  const data = ct.includes("json") ? await res.json() : await res.text();
  if (!res.ok) {
    const e = (data && data.error) || {
      code: "HTTP_" + res.status,
      message: (data && data.detail && JSON.stringify(data.detail)) || `请求失败（${res.status}）`,
    };
    const err = new ApiError(e);
    if (!quiet) toast(err.message, { hint: err.hint, error: true, ms: 6000 });
    throw err;
  }
  return data;
}

export async function download(path, body, filename) {
  const res = await fetch("/api" + path, {
    method: body ? "POST" : "GET",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const d = await res.json().catch(() => ({}));
    toast((d.error && d.error.message) || "下载失败", { error: true });
    return;
  }
  const blob = await res.blob();
  const cd = res.headers.get("content-disposition") || "";
  const m = cd.match(/filename\*=UTF-8''([^;]+)/);
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = m ? decodeURIComponent(m[1]) : filename || "download";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

export function sse(path, onEvent, onEnd) {
  const es = new EventSource("/api" + path);
  es.onmessage = (e) => onEvent(JSON.parse(e.data));
  es.addEventListener("end", (e) => {
    onEvent(JSON.parse(e.data));
    es.close();
    onEnd && onEnd();
  });
  return es;
}

export const store = {
  get(key, def) {
    try {
      const v = localStorage.getItem("bt:" + key);
      return v == null ? def : JSON.parse(v);
    } catch {
      return def;
    }
  },
  set(key, v) {
    try {
      localStorage.setItem("bt:" + key, JSON.stringify(v));
    } catch {}
  },
};

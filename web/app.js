import { api, download, sse, store, toast, toasts } from "./api.js";

const { createApp, reactive, ref, computed, watch, onMounted, onUnmounted, nextTick } = Vue;

// ---------------- 常量与工具 ----------------

const STAGE = {
  queued: "排队中", parsing: "解析", subtitle: "查找字幕", downloading: "下载音频",
  transcribing: "识别中", exporting: "导出", done: "完成", failed: "失败", cancelled: "已取消",
};
const SOURCE = {
  subtitle_cc: "CC 字幕", subtitle_ai: "AI 字幕", subtitle_yt: "YouTube 字幕", subtitle_yt_auto: "YouTube 自动字幕",
  asr_local: "本地识别", asr_remote: "GPU 识别",
};
const PLATFORM = { bilibili: "B站", youtube: "YouTube", local: "本地文件" };
const LANGS = [
  ["auto", "自动（按视频语言）"], ["zh", "中文"], ["en", "英语"], ["ja", "日语"], ["ko", "韩语"], ["yue", "粤语"],
];
const LANG_LABEL = Object.fromEntries(LANGS);
const FINAL = ["done", "failed", "cancelled"];
// fun-asr-nano 在 worker 上缺 model.py，修好前不开放（见问题文档 #006/#014）
const MODELS = { local: ["paraformer-zh"], remote: ["paraformer-zh"] };
const MODEL_LABEL = { "paraformer-zh": "paraformer-zh（推荐）", "fun-asr-nano": "fun-asr-nano（实验性，首次加载要几分钟）" };
const MEDIA_EXTS = [".mp4", ".mkv", ".flv", ".mov", ".avi", ".webm", ".m4s", ".ts",
  ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma", ".amr"];

const clock = (ms) => {
  const s = Math.max(0, Math.floor((ms || 0) / 1000));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  const p = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${p(m)}:${p(r)}` : `${p(m)}:${p(r)}`;
};
const dur = (sec) => clock((sec || 0) * 1000);
const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false }) : "");
const isActive = (t) => t && !FINAL.includes(t.status);
const taskTitle = (t) => (t.part_title ? `${t.title} · P${t.part} ${t.part_title}` : `${t.title}${t.part > 1 ? " · P" + t.part : ""}`);
const escapeHtml = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const platformOf = (t) => (t && t.platform) || (t && t.bvid === "LOCAL" ? "local" : "bilibili");
const isLocal = (t) => platformOf(t) === "local";
// 视频地址；ms 有值时跳到对应时间点
const bvUrl = (t, ms) => {
  const p = platformOf(t);
  if (p === "local") return null;
  const sec = ms != null ? Math.floor(ms / 1000) : null;
  if (p === "youtube") return `https://www.youtube.com/watch?v=${t.bvid}${sec != null ? "&t=" + sec + "s" : ""}`;
  return `https://www.bilibili.com/video/${t.bvid}?p=${t.part}${sec != null ? "&t=" + sec : ""}`;
};
// 这次识别会不会走中文模型（决定热词、说话人分离能不能用）
const isZhAsr = (lang, platform, videoLang) => {
  if (lang === "zh") return true;
  if (lang !== "auto") return false;
  if (platform !== "youtube") return true;
  return !!videoLang && videoLang.toLowerCase().startsWith("zh");
};

// ---------------- 全局状态 ----------------

const state = reactive({
  route: { name: "home", id: null },
  status: null,
  remote: { online: false, configured: false },
  login: null,
  tasks: {}, // id -> task（由全局 SSE 维护）
});

function parseRoute() {
  const h = location.hash.replace(/^#\/?/, "");
  const [name, id] = h.split("/");
  state.route = { name: name || "home", id: id ? Number(id) : null };
}
window.addEventListener("hashchange", parseRoute);
parseRoute();

function upsertTask(t) {
  state.tasks[t.id] = { ...(state.tasks[t.id] || {}), ...t };
}

let globalEs;
function connectGlobal() {
  globalEs = sse("/events", (ev) => {
    if (ev.type === "task") upsertTask(ev.task);
    else if (ev.type === "progress" && state.tasks[ev.id]) {
      Object.assign(state.tasks[ev.id], { percent: ev.percent, eta: ev.eta, ...(ev.message != null ? { message: ev.message } : {}) });
    } else if (ev.type === "deleted") delete state.tasks[ev.id];
  });
  globalEs.onerror = () => {}; // 浏览器会自动重连
}

async function refreshStatus() {
  try {
    state.status = await api("/status", { quiet: true });
  } catch {
    state.status = null;
  }
}
async function refreshRemote() {
  if (!state.status || !state.status.remote_configured) {
    state.remote = { online: false, configured: false };
    return;
  }
  try {
    state.remote = await api("/remote/health", { quiet: true });
  } catch {
    state.remote = { online: false, configured: true };
  }
}
async function refreshLogin() {
  try {
    state.login = await api("/settings/cookies/check", { quiet: true });
  } catch {
    state.login = null;
  }
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制到剪贴板");
  } catch {
    toast("复制失败，请手动选择文本", { error: true });
  }
}

// ---------------- 组件：热词输入 ----------------

const TagInput = {
  props: ["modelValue", "placeholder"],
  emits: ["update:modelValue"],
  setup(props, { emit }) {
    const draft = ref("");
    const add = () => {
      const words = draft.value.split(/[\s,，、]+/).map((w) => w.trim()).filter(Boolean);
      if (!words.length) return;
      const next = [...props.modelValue];
      for (const w of words) if (!next.includes(w)) next.push(w);
      emit("update:modelValue", next);
      draft.value = "";
    };
    const remove = (i) => {
      const next = [...props.modelValue];
      next.splice(i, 1);
      emit("update:modelValue", next);
    };
    const onKey = (e) => {
      if (["Enter", ",", "，", " "].includes(e.key)) {
        e.preventDefault();
        add();
      } else if (e.key === "Backspace" && !draft.value && props.modelValue.length) remove(props.modelValue.length - 1);
    };
    return { draft, add, remove, onKey };
  },
  template: `
    <div class="tags">
      <span class="tag" v-for="(w, i) in modelValue" :key="w">{{ w }}<b @click="remove(i)">×</b></span>
      <input v-model="draft" @keydown="onKey" @blur="add" :placeholder="modelValue.length ? '' : placeholder">
    </div>`,
};

// ---------------- 组件：任务卡片 ----------------

const TaskItem = {
  props: ["task"],
  setup(props) {
    const cancel = async () => {
      await api(`/tasks/${props.task.id}/cancel`, { method: "POST" });
      toast(props.task.status === "transcribing" ? "已请求取消，识别中途无法打断，当前识别结束后停止" : "已请求取消",
        { ms: 6000 });
    };
    const retry = async () => {
      const r = await api(`/tasks/${props.task.id}/retry`, { method: "POST", body: {} });
      toast("已重新加入队列");
      location.hash = `#/task/${r.id}`;
    };
    const remove = async () => {
      if (!confirm("删除这条任务及其结果？")) return;
      await api(`/tasks/${props.task.id}`, { method: "DELETE" });
    };
    return { STAGE, SOURCE, PLATFORM, platformOf, isActive, taskTitle, clock, cancel, retry, remove };
  },
  template: `
    <div class="task">
      <div class="info">
        <a class="title" :href="'#/task/' + task.id" :title="taskTitle(task)">{{ taskTitle(task) }}</a>
        <div class="sub">
          <span class="chip plat" :class="platformOf(task)">{{ PLATFORM[platformOf(task)] }}</span>
          <span class="chip" :class="{accent: isActive(task), err: task.status==='failed', sub: task.status==='done'}">{{ STAGE[task.status] || task.status }}</span>
          <span v-if="task.source" class="chip">{{ SOURCE[task.source] }}</span>
          <span v-if="task.duration_ms">{{ clock(task.duration_ms) }}</span>
          <span v-if="isActive(task) && task.message">{{ task.message }}</span>
          <span v-if="isActive(task) && task.percent > 0">{{ Math.floor(task.percent) }}%</span>
          <span v-if="isActive(task) && task.eta">约剩 {{ clock(task.eta * 1000) }}</span>
        </div>
        <div class="bar" v-if="isActive(task)"><i :style="{width: (task.percent || 0) + '%'}"></i></div>
        <div class="err" v-if="task.status==='failed' && task.error">{{ task.error.message }}<span v-if="task.error.hint"> — {{ task.error.hint }}</span></div>
      </div>
      <div class="acts">
        <button class="btn sm" v-if="isActive(task)" @click="cancel">取消</button>
        <button class="btn sm" v-if="task.status==='failed' || task.status==='cancelled'" @click="retry">重试</button>
        <a class="btn sm" v-if="task.status==='done'" :href="'#/task/' + task.id">查看</a>
        <button class="btn sm danger hide-sm" v-if="!isActive(task)" @click="remove">删除</button>
      </div>
    </div>`,
};

// ---------------- 页面：提交 ----------------

const Home = {
  components: { TagInput, TaskItem },
  setup() {
    const text = ref("");
    const parsing = ref(false);
    const parsed = ref(null);
    const parseErr = ref(null);
    const selected = ref([]);
    const submitting = ref(false);
    const groups = ref([]);
    const opts = reactive(Object.assign(
      { force_asr: false, spk: false, backend: "local", model: "paraformer-zh", lang: "auto", hotwords: [], groups: [] },
      store.get("opts", {}),
    ));
    const recentIds = ref([]);
    // 实时推送和提交返回都会加同一个任务 ID，先到的加、后到的跳过
    const addRecent = (ids) => {
      recentIds.value = [...new Set([...ids, ...recentIds.value])];
    };
    const file = ref(null);
    const fileInput = ref(null);
    const dragging = ref(false);
    const uploadPct = ref(0);

    function pickFile(f) {
      if (!f) return;
      const ext = (f.name.match(/\.[^.]+$/) || [""])[0].toLowerCase();
      if (!MEDIA_EXTS.includes(ext)) {
        toast(`不支持的文件类型 ${ext || "（无扩展名）"}`, { hint: "支持常见视频和音频文件", error: true });
        return;
      }
      file.value = f;
      text.value = "";
    }
    const onFileInput = (e) => { pickFile(e.target.files[0]); e.target.value = ""; };
    const onDrop = (e) => { dragging.value = false; pickFile(e.dataTransfer.files[0]); };
    const fileSize = (n) => (n >= 1 << 30 ? (n / (1 << 30)).toFixed(2) + " GB" : (n / (1 << 20)).toFixed(1) + " MB");

    // 记住的参数里可能有已下线的模型
    if (!MODELS[opts.backend] || !MODELS[opts.backend].includes(opts.model)) opts.model = "paraformer-zh";
    watch(opts, () => store.set("opts", { ...opts }), { deep: true });
    watch(() => opts.backend, (b) => { if (!MODELS[b].includes(opts.model)) opts.model = MODELS[b][0]; });

    let timer, seq = 0;
    watch(text, (v) => {
      clearTimeout(timer);
      if (v.trim()) file.value = null;
      if (!v.trim()) {
        parsed.value = null;
        parseErr.value = null;
        return;
      }
      timer = setTimeout(() => doParse(v), 500);
    });

    async function doParse(v) {
      const my = ++seq;
      parsing.value = true;
      parseErr.value = null;
      try {
        const r = await api("/parse", { method: "POST", body: { text: v }, quiet: true });
        if (my !== seq) return;
        parsed.value = r;
        const req = r.requested_p;
        selected.value = req ? [req] : r.parts.length === 1 ? [r.parts[0].index] : r.parts.map((p) => p.index);
      } catch (e) {
        if (my !== seq) return;
        parsed.value = null;
        parseErr.value = e;
      } finally {
        if (my === seq) parsing.value = false;
      }
    }

    const allChecked = computed(() => parsed.value && selected.value.length === parsed.value.parts.length);
    const toggleAll = () => {
      selected.value = allChecked.value ? [] : parsed.value.parts.map((p) => p.index);
    };
    const invert = () => {
      const s = new Set(selected.value);
      selected.value = parsed.value.parts.map((p) => p.index).filter((i) => !s.has(i));
    };

    const remoteOff = computed(() => !state.remote.online);
    const curPlatform = computed(() => (file.value ? "local" : parsed.value ? parsed.value.platform : "bilibili"));
    const zhAsr = computed(() => isZhAsr(opts.lang, curPlatform.value, parsed.value && parsed.value.language));

    function hotwordList() {
      const words = [...opts.hotwords];
      for (const g of groups.value) if (opts.groups.includes(g.name)) for (const w of g.words) if (!words.includes(w)) words.push(w);
      return words;
    }

    // fetch 拿不到上传进度，用 XHR
    function uploadFile() {
      return new Promise((resolve, reject) => {
        const form = new FormData();
        form.append("file", file.value);
        form.append("hotwords", JSON.stringify(hotwordList()));
        form.append("spk", opts.spk);
        form.append("backend", opts.backend);
        form.append("model", opts.model);
        form.append("lang", opts.lang);
        const xhr = new XMLHttpRequest();
        xhr.open("POST", "/api/upload");
        xhr.upload.onprogress = (e) => { if (e.lengthComputable) uploadPct.value = Math.floor((e.loaded * 100) / e.total); };
        xhr.onload = () => {
          let data = {};
          try { data = JSON.parse(xhr.responseText); } catch {}
          if (xhr.status === 200) resolve(data);
          else reject((data && data.error) || { message: `上传失败（${xhr.status}）` });
        };
        xhr.onerror = () => reject({ message: "上传失败：无法连接本地服务" });
        xhr.send(form);
      });
    }

    async function submit() {
      if (file.value) {
        submitting.value = true;
        uploadPct.value = 0;
        try {
          const r = await uploadFile();
          toast("已上传，开始转录");
          addRecent(r.ids);
          file.value = null;
        } catch (e) {
          toast(e.message, { hint: e.hint, error: true, ms: 6000 });
        } finally {
          submitting.value = false;
        }
        return;
      }
      if (!parsed.value || !selected.value.length) return;
      submitting.value = true;
      try {
        const words = hotwordList();
        const r = await api("/tasks", {
          method: "POST",
          body: {
            text: text.value, parts: selected.value, force_asr: opts.force_asr, spk: opts.spk,
            backend: opts.backend, model: opts.model, hotwords: words, lang: opts.lang,
          },
        });
        toast(`已创建 ${r.ids.length} 个任务`);
        addRecent(r.ids.slice().reverse());
        text.value = "";
      } finally {
        submitting.value = false;
      }
    }

    const recent = computed(() => recentIds.value.map((id) => state.tasks[id]).filter(Boolean));

    onMounted(async () => {
      const [list, s] = await Promise.all([api("/tasks?size=15"), api("/settings")]);
      list.items.forEach(upsertTask);
      recentIds.value = list.items.map((t) => t.id);
      groups.value = s.hotword_groups || [];
      if (!store.get("opts", null)) {
        Object.assign(opts, { backend: s.default_backend, model: s.default_model, spk: s.default_spk, force_asr: s.default_force_asr,
          lang: s.default_lang || "auto" });
      }
    });
    // 别处（重试等）新建的任务也显示出来
    watch(() => Object.keys(state.tasks).length, () => {
      const known = new Set(recentIds.value);
      const fresh = Object.values(state.tasks).filter((t) => !known.has(t.id) && isActive(t)).map((t) => t.id);
      if (fresh.length) addRecent(fresh.sort((a, b) => b - a));
    });

    return { text, parsing, parsed, parseErr, selected, opts, groups, submitting, submit, recent, allChecked,
      toggleAll, invert, dur, remoteOff, MODELS, MODEL_LABEL, state, file, fileInput, dragging, uploadPct, onFileInput, onDrop,
      fileSize, LANGS, LANG_LABEL, PLATFORM, zhAsr, curPlatform };
  },
  template: `
    <div class="card" :class="{dragging}" @dragover.prevent="dragging = true" @dragleave.self="dragging = false" @drop.prevent="onDrop">
      <h2>转录 B 站 / YouTube 视频</h2>
      <textarea class="url" v-model="text" placeholder="粘贴 B 站链接、BV 号、b23.tv 短链，或 YouTube 链接（youtube.com / youtu.be）"></textarea>
      <div class="row" style="margin-top:10px">
        <span class="muted">或</span>
        <button class="btn sm" @click="fileInput.click()">上传本地音视频文件</button>
        <span class="muted">也可以直接把文件拖进这里</span>
        <input type="file" ref="fileInput" style="display:none" accept="video/*,audio/*,.flv,.mkv,.m4s" @change="onFileInput">
      </div>

      <div v-if="file" class="filebox">
        <span class="chip accent">本地文件</span>
        <span class="t grow" :title="file.name">{{ file.name }}</span>
        <span class="muted">{{ fileSize(file.size) }}</span>
        <button class="btn sm" @click="file = null" :disabled="submitting">移除</button>
      </div>
      <div v-else-if="parsing" class="skeleton" style="margin-top:14px"></div>
      <div v-else-if="parseErr" class="err" style="color:var(--err);margin-top:10px">
        {{ parseErr.message }}<span v-if="parseErr.hint" class="muted"> — {{ parseErr.hint }}</span>
      </div>
      <template v-else-if="parsed">
        <div class="sep"></div>
        <div class="video">
          <img v-if="parsed.cover" :src="parsed.cover" referrerpolicy="no-referrer" alt="">
          <div class="meta grow">
            <h3><span class="chip plat" :class="parsed.platform">{{ PLATFORM[parsed.platform] }}</span> {{ parsed.title }}</h3>
            <div class="muted" v-if="parsed.platform === 'youtube'">
              {{ parsed.bvid }} · 频道 {{ parsed.uploader || '-' }} · 原语言 {{ parsed.language || '未标注' }}{{ parsed.language && !parsed.language_tagged ? '（按标题推断）' : '' }}
              <template v-if="parsed.subs"> · 人工字幕 {{ parsed.subs.manual.length ? parsed.subs.manual.join(', ') : '无' }} · 原语言自动字幕 {{ parsed.subs.auto ? '有' : '无' }}</template>
            </div>
            <div class="muted" v-else>{{ parsed.bvid }} · UP 主 {{ parsed.uploader || '-' }} · 共 {{ parsed.parts.length }} P</div>
            <div class="row" style="margin-top:8px" v-if="parsed.parts.length > 1">
              <button class="btn sm" @click="toggleAll">{{ allChecked ? '全不选' : '全选' }}</button>
              <button class="btn sm" @click="invert">反选</button>
              <span class="muted">已选 {{ selected.length }} 个分P</span>
            </div>
          </div>
        </div>
        <div class="parts">
          <label v-for="p in parsed.parts" :key="p.index">
            <input type="checkbox" :value="p.index" v-model="selected">
            <span class="chip" v-if="parsed.platform !== 'youtube'">P{{ p.index }}</span>
            <span class="t" :title="p.title">{{ p.title }}</span>
            <span class="chip sub" v-if="p.subtitles.length" :title="p.subtitles.join(', ')">有字幕</span>
            <span class="muted">{{ dur(p.duration) }}</span>
          </label>
        </div>
      </template>

      <template v-if="file || (parsed && !parsing && !parseErr)">
        <div class="opts">
          <label class="check" v-if="!file"><input type="checkbox" v-model="opts.force_asr"> 强制转录（忽略字幕）</label>
          <label class="check" :title="zhAsr ? '' : '多语言模型 SenseVoice 不支持区分说话人'">
            <input type="checkbox" v-model="opts.spk" :disabled="!zhAsr"> 区分说话人（较慢{{ zhAsr ? '' : '，仅中文' }}）</label>
          <div>
            <select v-model="opts.lang" title="识别语言：中文用 paraformer，英日韩粤用 SenseVoice">
              <option v-for="[v, l] in LANGS" :value="v">识别语言：{{ l }}</option>
            </select>
          </div>
          <div>
            <select v-model="opts.backend">
              <option value="local">本地 CPU 识别</option>
              <option value="remote" :disabled="remoteOff">远程 GPU 识别{{ remoteOff ? '（离线）' : '' }}</option>
            </select>
          </div>
          <div v-if="zhAsr">
            <select v-model="opts.model">
              <option v-for="m in MODELS[opts.backend]" :value="m">{{ MODEL_LABEL[m] || m }}</option>
            </select>
          </div>
        </div>
        <div class="muted" v-if="!zhAsr" style="margin:-6px 0 10px">非中文内容用多语言模型 SenseVoice 识别：不支持热词和区分说话人。有字幕的视频会优先直接用字幕。</div>
        <div class="field" v-if="zhAsr">
          <span class="label">热词（回车添加，专业名词、人名等）</span>
          <TagInput v-model="opts.hotwords" placeholder="例如：犬瘟热 细小病毒"></TagInput>
          <div class="row" style="margin-top:8px" v-if="groups.length">
            <span class="muted">热词组：</span>
            <label class="check" v-for="g in groups" :key="g.name"><input type="checkbox" :value="g.name" v-model="opts.groups"> {{ g.name }}（{{ g.words.length }}）</label>
          </div>
        </div>
        <div class="row">
          <button class="btn primary" v-if="file" :disabled="submitting" @click="submit">{{ submitting ? '上传中 ' + uploadPct + '%' : '上传并转录' }}</button>
          <button class="btn primary" v-else :disabled="!selected.length || submitting" @click="submit">开始转录（{{ selected.length }}）</button>
          <span class="muted" v-if="!file && !opts.force_asr">有字幕时会直接使用字幕{{ curPlatform === 'youtube' ? '（人工字幕优先，其次原语言自动字幕）' : '' }}</span>
        </div>
      </template>
    </div>

    <div class="card">
      <div class="row"><h2 class="grow" style="margin:0">最近任务</h2><a href="#/history" class="muted">全部历史 →</a></div>
      <div v-if="!recent.length" class="empty">还没有任务</div>
      <TaskItem v-for="t in recent" :key="t.id" :task="t"></TaskItem>
    </div>`,
};

// ---------------- 页面：结果 ----------------

const TaskView = {
  components: { TaskItem },
  props: ["id"],
  setup(props) {
    const result = ref(null);
    const loading = ref(true);
    const q = ref("");
    const cur = ref(0);
    const showLog = ref(false);
    const logText = ref("");
    let es;

    const task = computed(() => state.tasks[props.id]);

    async function load() {
      loading.value = true;
      try {
        const r = await api(`/tasks/${props.id}`);
        upsertTask(r.task);
        result.value = r.result;
        if (isActive(r.task)) {
          es && es.close();
          es = sse(`/tasks/${props.id}/events`, (ev) => {
            if (ev.type === "task") upsertTask(ev.task);
          }, () => setTimeout(load, 300));
        }
      } finally {
        loading.value = false;
      }
    }
    watch(() => props.id, load);
    onMounted(load);
    onUnmounted(() => es && es.close());

    const hits = computed(() => {
      const k = q.value.trim();
      if (!k || !result.value) return [];
      return result.value.sentences.map((s, i) => (s.text.includes(k) ? i : -1)).filter((i) => i >= 0);
    });
    watch(q, () => { cur.value = 0; jump(); });
    function jump() {
      nextTick(() => {
        const i = hits.value[cur.value];
        if (i == null) return;
        const el = document.getElementById("s" + i);
        el && el.scrollIntoView({ block: "center", behavior: "smooth" });
      });
    }
    const step = (d) => {
      if (!hits.value.length) return;
      cur.value = (cur.value + d + hits.value.length) % hits.value.length;
      jump();
    };
    const hl = (text) => {
      const k = q.value.trim();
      const safe = escapeHtml(text);
      if (!k) return safe;
      return safe.split(escapeHtml(k)).join(`<mark>${escapeHtml(k)}</mark>`);
    };

    async function copyAll() {
      const res = await fetch(`/api/tasks/${props.id}/export?fmt=txt`);
      copyText(await res.text());
    }
    const dl = (fmt) => download(`/tasks/${props.id}/export?fmt=${fmt}`);
    async function retranscribe() {
      const r = await api(`/tasks/${props.id}/retry`, { method: "POST", body: { force_asr: true } });
      toast("已创建重新转录任务");
      location.hash = `#/task/${r.id}`;
    }
    async function toggleLog() {
      showLog.value = !showLog.value;
      if (showLog.value) logText.value = (await api(`/tasks/${props.id}/log`)) || "（暂无日志）";
    }

    return { task, result, loading, q, hits, cur, step, hl, copyAll, dl, retranscribe, toggleLog, showLog, logText,
      clock, SOURCE, isActive, taskTitle, bvUrl, isLocal, platformOf, PLATFORM };
  },
  template: `
    <div v-if="!task && loading" class="skeleton"></div>
    <div v-else-if="!task" class="card empty">任务不存在</div>
    <template v-else>
      <div class="card">
        <div class="row" style="align-items:flex-start">
          <div class="grow">
            <h2 style="margin-bottom:4px">{{ taskTitle(task) }}</h2>
            <div class="muted">
              <template v-if="isLocal(task)">本地文件 · {{ task.opts && task.opts.filename }}</template>
              <template v-else><span class="chip plat" :class="platformOf(task)">{{ PLATFORM[platformOf(task)] }}</span>
              <a :href="bvUrl(task)" target="_blank" rel="noopener">{{ task.bvid }}{{ platformOf(task) === 'youtube' ? '' : ' P' + task.part }}</a>
              · {{ task.uploader || '-' }}</template>
              · {{ clock(task.duration_ms) }}
              <span v-if="result"> · {{ result.sentences.length }} 句</span>
            </div>
          </div>
          <span v-if="result" class="chip accent">{{ SOURCE[result.source] }}</span>
          <span v-if="result && result.model" class="chip">{{ result.model }}</span>
        </div>
        <div v-if="isActive(task) || task.status !== 'done'" style="margin-top:12px"><TaskItem :task="task"></TaskItem></div>
        <template v-if="result">
          <div class="sep"></div>
          <div class="toolbar">
            <button class="btn primary sm" @click="copyAll">复制全文</button>
            <button class="btn sm" @click="dl('txt')">下载 txt</button>
            <button class="btn sm" @click="dl('srt')" :title="result.no_timestamp ? '该结果没有时间戳' : ''">下载 srt</button>
            <button class="btn sm" @click="dl('md')">下载 md</button>
            <button class="btn sm" @click="retranscribe" v-if="result.source.startsWith('subtitle')">重新转录</button>
            <button class="btn sm" @click="toggleLog">{{ showLog ? '隐藏日志' : '查看日志' }}</button>
            <div class="grow"></div>
            <div style="width:220px"><input type="search" v-model="q" placeholder="页内搜索" @keydown.enter="step(1)"></div>
            <template v-if="q">
              <span class="muted">{{ hits.length ? (cur + 1) + '/' + hits.length : '无结果' }}</span>
              <button class="btn sm" @click="step(-1)">↑</button>
              <button class="btn sm" @click="step(1)">↓</button>
            </template>
          </div>
          <div class="muted" v-if="result.no_timestamp" style="margin-top:8px">该模型没有返回时间戳，srt 只有一条字幕。</div>
          <div class="muted" v-if="result.source === 'subtitle_yt_auto'" style="margin-top:8px">这是 YouTube 自动生成的字幕，通常没有标点、断句较碎；想要带标点的文本可以点「重新转录」。</div>
        </template>
        <pre class="log" v-if="showLog" style="margin-top:12px">{{ logText }}</pre>
      </div>

      <div class="card" v-if="result">
        <div class="sents">
          <div v-for="(s, i) in result.sentences" :key="i" :id="'s' + i" class="sent"
               :class="{hit: hits.includes(i), cur: hits[cur] === i}">
            <span class="ts" v-if="!result.no_timestamp && isLocal(task)">{{ clock(s.start) }}</span>
            <a class="ts" v-else-if="!result.no_timestamp" :href="bvUrl(task, s.start)" target="_blank" rel="noopener">{{ clock(s.start) }}</a>
            <span class="spk" v-if="s.spk != null" :style="{color: 'var(--spk-' + (s.spk % 5) + ')'}">说话人{{ s.spk + 1 }}</span>
            <span class="tx" v-html="hl(s.text)"></span>
          </div>
        </div>
      </div>
    </template>`,
};

// ---------------- 页面：历史 ----------------

const History = {
  setup() {
    const q = ref("");
    const status = ref("");
    const platform = ref("");
    const page = ref(1);
    const data = ref({ items: [], total: 0, size: 20 });
    const selected = ref([]);
    const fmt = ref("txt");

    async function load() {
      const p = new URLSearchParams({ q: q.value, status: status.value, platform: platform.value, page: page.value, size: 20 });
      data.value = await api("/tasks?" + p);
      data.value.items.forEach(upsertTask);
      selected.value = selected.value.filter((id) => data.value.items.some((t) => t.id === id));
    }
    let timer;
    watch(q, () => { clearTimeout(timer); timer = setTimeout(() => { page.value = 1; load(); }, 300); });
    watch([status, platform], () => { page.value = 1; load(); });
    watch(page, load);
    onMounted(load);

    const rows = computed(() => data.value.items.map((t) => state.tasks[t.id] || t));
    const pages = computed(() => Math.max(1, Math.ceil(data.value.total / data.value.size)));
    const allChecked = computed(() => rows.value.length && selected.value.length === rows.value.length);
    const toggleAll = () => { selected.value = allChecked.value ? [] : rows.value.map((t) => t.id); };

    const zip = () => download("/export/zip", { task_ids: selected.value, fmt: fmt.value }, "transcripts.zip");
    async function removeSel() {
      if (!confirm(`删除选中的 ${selected.value.length} 条任务？`)) return;
      await api("/tasks/delete", { method: "POST", body: { ids: selected.value } });
      selected.value = [];
      load();
    }

    return { q, status, platform, page, pages, data, rows, selected, fmt, zip, removeSel, allChecked, toggleAll,
      STAGE, SOURCE, PLATFORM, platformOf, clock, when, taskTitle };
  },
  template: `
    <div class="card">
      <div class="row" style="margin-bottom:12px">
        <div class="grow"><input type="search" v-model="q" placeholder="按标题、BV 号或 YouTube 视频 ID 搜索"></div>
        <div style="width:120px">
          <select v-model="platform">
            <option value="">全部平台</option><option value="bilibili">B站</option>
            <option value="youtube">YouTube</option><option value="local">本地文件</option>
          </select>
        </div>
        <div style="width:140px">
          <select v-model="status">
            <option value="">全部状态</option><option value="active">进行中</option>
            <option value="done">完成</option><option value="failed">失败</option><option value="cancelled">已取消</option>
          </select>
        </div>
      </div>
      <div class="row" style="margin-bottom:8px" v-if="selected.length">
        <span class="muted">已选 {{ selected.length }} 条</span>
        <div style="width:110px"><select v-model="fmt"><option value="txt">txt</option><option value="srt">srt</option><option value="md">md</option><option value="all">全部格式</option></select></div>
        <button class="btn sm primary" @click="zip">批量导出 zip</button>
        <button class="btn sm danger" @click="removeSel">批量删除</button>
      </div>
      <div v-if="!rows.length" class="empty">没有记录</div>
      <table v-else>
        <thead><tr>
          <th style="width:32px"><input type="checkbox" :checked="allChecked" @change="toggleAll"></th>
          <th>标题</th><th class="hide-sm">来源</th><th>状态</th><th class="hide-sm">时长</th><th class="hide-sm">创建时间</th>
        </tr></thead>
        <tbody>
          <tr v-for="t in rows" :key="t.id">
            <td><input type="checkbox" :value="t.id" v-model="selected"></td>
            <td class="title"><span class="chip plat" :class="platformOf(t)">{{ PLATFORM[platformOf(t)] }}</span>
              <a :href="'#/task/' + t.id" :title="taskTitle(t)">{{ taskTitle(t) }}</a></td>
            <td class="hide-sm"><span class="chip" v-if="t.source">{{ SOURCE[t.source] }}</span></td>
            <td><span class="chip" :class="{sub: t.status==='done', err: t.status==='failed'}">{{ STAGE[t.status] }}</span></td>
            <td class="hide-sm muted">{{ clock(t.duration_ms) }}</td>
            <td class="hide-sm muted">{{ when(t.created_at) }}</td>
          </tr>
        </tbody>
      </table>
      <div class="pager" v-if="pages > 1">
        <button class="btn sm" :disabled="page<=1" @click="page--">上一页</button>
        <span class="muted">{{ page }} / {{ pages }}</span>
        <button class="btn sm" :disabled="page>=pages" @click="page++">下一页</button>
      </div>
    </div>`,
};

// ---------------- 页面：设置 ----------------

const SettingsView = {
  components: { TagInput },
  setup() {
    const s = ref(null);
    const saving = ref(false);
    const testing = ref(false);
    const testResult = ref(null);
    const fileInput = ref(null);
    const ytFileInput = ref(null);
    const updating = ref(false);
    const proxyTesting = ref(false);
    const proxyResult = ref(null);

    async function testProxy() {
      proxyTesting.value = true;
      try {
        proxyResult.value = await api("/settings/proxy/check", { method: "POST", body: { proxy: s.value.youtube_proxy } });
      } finally {
        proxyTesting.value = false;
      }
    }
    async function uploadYt(e) {
      const f = e.target.files[0];
      if (!f) return;
      const form = new FormData();
      form.append("file", f);
      s.value = await api("/settings/youtube-cookies", { method: "POST", form });
      toast("YouTube cookies.txt 已导入");
      e.target.value = "";
    }
    async function clearYt() {
      s.value = await api("/settings/youtube-cookies", { method: "DELETE" });
      toast("YouTube Cookie 已清除");
    }

    async function load() {
      s.value = await api("/settings");
    }
    onMounted(() => { load(); refreshLogin(); });

    async function save(patch, msg = "已保存") {
      saving.value = true;
      try {
        s.value = await api("/settings", { method: "PUT", body: patch || s.value });
        toast(msg);
        refreshStatus().then(refreshRemote);
      } finally {
        saving.value = false;
      }
    }
    async function saveCookie() {
      await save({ sessdata: s.value.sessdata, bili_jct: s.value.bili_jct, dedeuserid: s.value.dedeuserid, buvid3: s.value.buvid3 }, "Cookie 已保存");
      refreshLogin();
    }
    async function upload(e) {
      const f = e.target.files[0];
      if (!f) return;
      const form = new FormData();
      form.append("file", f);
      s.value = await api("/settings/cookies", { method: "POST", form });
      toast("cookies.txt 已导入");
      e.target.value = "";
      refreshLogin();
    }
    async function clearCookie() {
      s.value = await api("/settings/cookies", { method: "DELETE" });
      toast("Cookie 已清除");
      refreshLogin();
    }
    async function test() {
      testing.value = true;
      try {
        testResult.value = await api("/remote/health", { method: "POST", body: { url: s.value.remote_url, token: s.value.remote_token } });
      } finally {
        testing.value = false;
      }
    }
    const addGroup = () => s.value.hotword_groups.push({ name: "新热词组", words: [] });
    const delGroup = (i) => s.value.hotword_groups.splice(i, 1);
    async function clearCache() {
      const r = await api("/maintenance/clear-cache", { method: "POST" });
      toast(`已清理 ${r.removed} 个缓存文件`);
    }
    async function updateYtdlp() {
      updating.value = true;
      try {
        const r = await api("/maintenance/update-ytdlp", { method: "POST" });
        toast("下载组件已更新", { hint: r.hint, ms: 6000 });
      } finally {
        updating.value = false;
      }
    }
    return { s, saving, save, saveCookie, upload, clearCookie, fileInput, test, testing, testResult, addGroup, delGroup,
      clearCache, updateYtdlp, updating, state, MODELS, LANGS, ytFileInput, uploadYt, clearYt, testProxy, proxyTesting,
      proxyResult };
  },
  template: `
    <div v-if="!s" class="skeleton"></div>
    <template v-else>
      <div class="card">
        <h2>B 站 Cookie</h2>
        <p class="muted" style="margin-top:-6px">AI 字幕、高码率和部分视频需要登录。在浏览器登录 B 站 → F12 → 应用 → Cookie → 复制 SESSDATA。Cookie 只保存在本机。</p>
        <div class="row" style="margin-bottom:12px">
          <span v-if="state.login && state.login.is_login" class="chip sub">已登录：{{ state.login.uname }}</span>
          <span v-else-if="state.login && state.login.configured" class="chip err">{{ state.login.error || '未登录' }}</span>
          <span v-else class="chip">未配置</span>
          <span class="muted" v-if="s.cookie_source === 'file'">来源：cookies.txt</span>
        </div>
        <div class="field"><span class="label">SESSDATA（必填）</span><input type="password" v-model="s.sessdata" autocomplete="off" placeholder="粘贴 SESSDATA"></div>
        <div class="field"><span class="label">buvid3（必填，同一个 Cookie 列表里，缺了 B 站会返回 412）</span><input type="text" v-model="s.buvid3" autocomplete="off" placeholder="形如 XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXXinfoc"></div>
        <div class="row">
          <div class="field grow"><span class="label">bili_jct（可选）</span><input type="password" v-model="s.bili_jct" autocomplete="off"></div>
          <div class="field grow"><span class="label">DedeUserID（可选）</span><input type="text" v-model="s.dedeuserid" autocomplete="off"></div>
        </div>
        <div class="row">
          <button class="btn primary" @click="saveCookie" :disabled="saving">保存并校验</button>
          <button class="btn" @click="fileInput.click()">上传 cookies.txt</button>
          <input type="file" ref="fileInput" accept=".txt" style="display:none" @change="upload">
          <button class="btn danger" @click="clearCookie" v-if="s.cookie_configured">清除</button>
        </div>
      </div>

      <div class="card">
        <h2>YouTube</h2>
        <p class="muted" style="margin-top:-6px">国内访问 YouTube 需要代理。只有 YouTube 的请求走这个代理，B 站照常直连。Clash Verge 默认 <code>http://127.0.0.1:7897</code>，Clash for Windows 默认 <code>7890</code>，v2rayN 默认 <code>10808</code>（socks5）/ <code>10809</code>（http）。</p>
        <div class="field"><span class="label">代理地址</span><input type="text" v-model="s.youtube_proxy" placeholder="http://127.0.0.1:7897 或 socks5://127.0.0.1:10808"></div>
        <div class="row" style="margin-bottom:14px">
          <button class="btn primary" @click="save({youtube_proxy: s.youtube_proxy})" :disabled="saving">保存</button>
          <button class="btn" @click="testProxy" :disabled="proxyTesting">{{ proxyTesting ? '测试中…' : '测试代理' }}</button>
          <span v-if="proxyResult && proxyResult.ok" class="chip sub">能连上 YouTube · {{ proxyResult.latency_ms }}ms</span>
          <span v-else-if="proxyResult" class="chip err">连不上：{{ proxyResult.error }}</span>
        </div>
        <p class="muted">遇到「Sign in to confirm you're not a bot」或年龄限制时，用浏览器扩展（如 Get cookies.txt LOCALLY）在已登录的 youtube.com 导出 cookies.txt 上传。只保留 youtube.com / google.com 的条目，和 B 站 Cookie 分开存放。</p>
        <div class="row">
          <span v-if="s.youtube_cookie" class="chip sub">已上传 Cookie</span>
          <span v-else class="chip">未配置 Cookie（可选）</span>
          <button class="btn" @click="ytFileInput.click()">上传 cookies.txt</button>
          <input type="file" ref="ytFileInput" accept=".txt" style="display:none" @change="uploadYt">
          <button class="btn danger" @click="clearYt" v-if="s.youtube_cookie">清除</button>
        </div>
      </div>

      <div class="card">
        <h2>识别默认值</h2>
        <div class="opts" style="margin-top:0">
          <div><span class="muted">默认后端</span>
            <select v-model="s.default_backend"><option value="local">本地 CPU</option><option value="remote">远程 GPU</option></select></div>
          <div><span class="muted">默认中文模型</span>
            <select v-model="s.default_model"><option v-for="m in MODELS[s.default_backend]" :value="m">{{ m }}</option></select></div>
          <div><span class="muted">默认识别语言</span>
            <select v-model="s.default_lang"><option v-for="[v, l] in LANGS" :value="v">{{ l }}</option></select></div>
        </div>
        <div class="row" style="gap:20px">
          <label class="check"><input type="checkbox" v-model="s.default_spk"> 默认区分说话人</label>
          <label class="check"><input type="checkbox" v-model="s.default_force_asr"> 默认强制转录</label>
          <label class="check"><input type="checkbox" v-model="s.auto_delete_audio"> 完成后自动删除音频</label>
        </div>
        <p class="muted">实测速度：本地中文 RTF {{ s.rtf_local }}，本地多语言 RTF {{ s.rtf_sensevoice }}，远程 RTF {{ s.rtf_remote }}（处理 1 秒音频所需的秒数，用于预估进度）</p>
        <button class="btn primary" @click="save()" :disabled="saving">保存</button>
      </div>

      <div class="card">
        <h2>远程 GPU（AutoDL）</h2>
        <p class="muted" style="margin-top:-6px">先在本地建 SSH 隧道：<code>ssh -CNg -L 9000:127.0.0.1:9000 root@&lt;地址&gt; -p &lt;端口&gt;</code></p>
        <div class="row">
          <div class="field grow"><span class="label">worker 地址</span><input type="text" v-model="s.remote_url"></div>
          <div class="field grow"><span class="label">Token（worker 的 ASR_TOKEN）</span><input type="password" v-model="s.remote_token" autocomplete="off"></div>
        </div>
        <div class="row">
          <button class="btn primary" @click="save()" :disabled="saving">保存</button>
          <button class="btn" @click="test" :disabled="testing">{{ testing ? '测试中…' : '测试连接' }}</button>
          <span v-if="testResult && testResult.online" class="chip sub">在线 · {{ testResult.gpu }} · {{ testResult.latency_ms }}ms</span>
          <span v-else-if="testResult" class="chip err">离线：{{ testResult.error || '未配置' }}</span>
        </div>
      </div>

      <div class="card">
        <h2>热词组</h2>
        <div class="group" v-for="(g, i) in s.hotword_groups" :key="i">
          <div class="row" style="margin-bottom:8px">
            <div class="grow"><input type="text" v-model="g.name" placeholder="组名"></div>
            <button class="btn sm danger" @click="delGroup(i)">删除</button>
          </div>
          <TagInput v-model="g.words" placeholder="输入热词后回车"></TagInput>
        </div>
        <div class="row">
          <button class="btn" @click="addGroup">新增热词组</button>
          <button class="btn primary" @click="save({hotword_groups: s.hotword_groups})" :disabled="saving">保存热词组</button>
        </div>
      </div>

      <div class="card">
        <h2>维护</h2>
        <div class="row">
          <button class="btn" @click="clearCache">清理音频缓存</button>
          <button class="btn" @click="updateYtdlp" :disabled="updating">{{ updating ? '更新中…' : '更新下载组件（yt-dlp）' }}</button>
          <span class="muted" v-if="state.status">当前 yt-dlp {{ state.status.ytdlp }}</span>
        </div>
      </div>
    </template>`,
};

// ---------------- 根组件 ----------------

const App = {
  components: { Home, TaskView, History, SettingsView },
  setup() {
    const timers = [];
    onMounted(() => {
      connectGlobal();
      refreshStatus().then(refreshRemote);
      refreshLogin();
      timers.push(setInterval(refreshStatus, 5000), setInterval(refreshRemote, 30000));
    });
    onUnmounted(() => timers.forEach(clearInterval));

    const modelDot = computed(() => {
      const m = state.status && state.status.model;
      if (!m) return ["", "模型未加载"];
      return { idle: ["", "模型待加载"], loading: ["warn", "模型加载中"], ready: ["ok", "模型就绪"], error: ["err", "模型出错"] }[m.status];
    });
    const remoteDot = computed(() => {
      if (!state.remote.configured) return ["", "远程未配置"];
      return state.remote.online ? ["ok", "远程在线"] : ["err", "远程离线"];
    });
    const cookieDot = computed(() => {
      const l = state.login;
      if (!l || !l.configured) return ["", "未登录 B 站"];
      return l.is_login ? ["ok", l.uname] : ["err", "Cookie 失效"];
    });
    return { state, toasts, modelDot, remoteDot, cookieDot };
  },
  template: `
    <header class="topbar">
      <div class="brand"><i></i>视频转录</div>
      <nav class="nav">
        <a href="#/" :class="{on: state.route.name==='home'}">转录</a>
        <a href="#/history" :class="{on: state.route.name==='history'}">历史</a>
        <a href="#/settings" :class="{on: state.route.name==='settings'}">设置</a>
      </nav>
      <div class="status">
        <span :title="state.status && state.status.model.message"><i class="dot" :class="modelDot[0]"></i>{{ modelDot[1] }}</span>
        <span><i class="dot" :class="remoteDot[0]"></i>{{ remoteDot[1] }}</span>
        <span><i class="dot" :class="cookieDot[0]"></i>{{ cookieDot[1] }}</span>
      </div>
    </header>
    <main>
      <div class="banner" v-if="!state.status">无法连接本地服务，请确认 start.bat 窗口仍在运行。</div>
      <div class="banner" v-else-if="!state.status.ffmpeg">没有找到 ffmpeg / ffprobe，请先安装：<code>winget install Gyan.FFmpeg</code>，然后重启服务。</div>
      <Home v-if="state.route.name==='home'"></Home>
      <TaskView v-else-if="state.route.name==='task'" :id="state.route.id"></TaskView>
      <History v-else-if="state.route.name==='history'"></History>
      <SettingsView v-else-if="state.route.name==='settings'"></SettingsView>
    </main>
    <div class="toasts">
      <div class="toast" v-for="t in toasts" :key="t.id" :class="{err: t.error}">{{ t.message }}<small v-if="t.hint">{{ t.hint }}</small></div>
    </div>`,
};

createApp(App).mount("#app");

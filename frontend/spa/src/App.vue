<template>
  <el-container class="layout-root">
    <el-aside width="220px" class="side-nav">
      <div class="brand">AI Test Workbench</div>
      <el-menu :default-active="activeMenu" @select="activeMenu = $event">
        <el-menu-item index="workspace">工作台</el-menu-item>
        <el-menu-item index="reports">报告中心</el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="topbar">
        <el-breadcrumb separator="/">
          <el-breadcrumb-item>AI 测试平台</el-breadcrumb-item>
          <el-breadcrumb-item>{{ activeMenu === "workspace" ? "工作台" : "报告中心" }}</el-breadcrumb-item>
        </el-breadcrumb>
        <el-tag :type="deviceOnline ? 'success' : 'danger'">
          {{ deviceOnline ? "设备在线" : "设备离线" }}
        </el-tag>
      </el-header>

      <el-main class="main-content workspace-main" v-if="activeMenu === 'workspace'">
        <div class="workspace-grid">
          <section class="panel panel-device">
            <div class="panel-header">
              <h3 class="panel-title">设备投屏</h3>
              <el-button size="small" @click="refreshSnapshot(true)">刷新</el-button>
            </div>
            <div class="preview">
              <img v-if="snapshotImage" :src="'data:image/png;base64,' + snapshotImage" alt="device" />
              <div v-else class="placeholder">暂无截图</div>
            </div>
            <div class="device-meta">
              <div><strong>页面语义：</strong>{{ formattedPageSummary }}</div>
              <div style="margin-top: 4px">
                <strong>主要路径入口：</strong>
                <span>{{ formattedPrimaryPaths }}</span>
              </div>
            </div>
            <el-button-group style="margin-top: 10px; width: 100%">
              <el-button style="width: 25%" @click="sendDeviceKey('home')">Home</el-button>
              <el-button style="width: 25%" @click="sendDeviceKey('back')">Back</el-button>
              <el-button style="width: 25%" @click="sendDeviceKey('recent')">Recent</el-button>
              <el-button style="width: 25%" @click="sendDeviceKey('power')">Power</el-button>
            </el-button-group>
            <div class="device-elements">
              <div class="sub-title">元素树</div>
              <div class="logs">
                <div v-for="(el, idx) in elementList.slice(0, 220)" :key="idx" class="log-row">
                  [{{ el.region || "content" }}/{{ el.role || "unknown" }}]
                  {{ el.label || el.text || el.content_desc || "(empty)" }}
                </div>
              </div>
            </div>
          </section>

          <section class="panel panel-chat">
            <div class="panel-header">
              <h3 class="panel-title">AI 对话与执行</h3>
              <el-tag size="small">{{ wsConnected ? "WS在线" : "HTTP模式" }}</el-tag>
            </div>
            <div class="chat-list">
              <div v-for="m in messages" :key="m.id" class="bubble" :class="m.type">
                <div class="bubble-title">{{ m.title }} · {{ m.time }}</div>
                <div class="bubble-content">{{ m.content }}</div>
              </div>
            </div>
            <el-input v-model="inputText" type="textarea" :rows="3" placeholder="输入测试指令..." />
            <div style="margin-top: 8px; text-align: right">
              <el-button type="primary" :loading="executing" @click="parseIntent">开始执行</el-button>
            </div>
          </section>

          <section class="panel panel-status">
            <div class="panel-header">
              <h3 class="panel-title">实时日志</h3>
            </div>
            <div class="logs status-logs">
              <div v-for="(log, idx) in logs" :key="idx" class="log-row">{{ log }}</div>
            </div>
          </section>
        </div>
      </el-main>

      <el-main class="main-content" v-else>
        <section class="panel">
          <div class="panel-header">
            <h3 class="panel-title">Allure 报告中心</h3>
            <el-button @click="openAllureReport">查看完整报告</el-button>
          </div>
          <el-table :data="reportTasks" border stripe>
            <el-table-column prop="name" label="任务名称" />
            <el-table-column prop="mode" label="模式" width="110" />
            <el-table-column prop="status" label="状态" width="110" />
            <el-table-column prop="duration_seconds" label="耗时(s)" width="120" />
            <el-table-column prop="created_at" label="创建时间" min-width="180" />
            <el-table-column label="操作" width="120">
              <template #default="{ row }">
                <el-button size="small" @click="openReportDetail(row)">查看</el-button>
              </template>
            </el-table-column>
          </el-table>
        </section>
      </el-main>
    </el-container>
  </el-container>

  <el-drawer v-model="intentDrawerVisible" title="测试计划确认" size="36%">
    <el-form label-width="100px">
      <el-form-item label="模式">
        <el-select v-model="editableIntent.intent">
          <el-option label="🏗️ 语义扫描" value="traverse" />
          <el-option label="▶️ 意图执行" value="run" />
          <el-option label="🔄 回放对比" value="replay" />
          <el-option label="📄 执行用例" value="run_case" />
          <el-option label="🧩 生成用例" value="generate_case" />
        </el-select>
      </el-form-item>
      <el-form-item label="应用名称"><el-input v-model="editableIntent.app_name" /></el-form-item>
      <el-form-item label="包名"><el-input v-model="editableIntent.app_package" /></el-form-item>
      <el-form-item label="用例文件"><el-input v-model="editableIntent.case_file" /></el-form-item>
      <el-form-item label="匹配用例" v-if="(editableIntent.case_candidates || []).length">
        <el-select v-model="editableIntent.case_file" filterable>
          <el-option
            v-for="item in editableIntent.case_candidates"
            :key="item"
            :label="item"
            :value="item"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="扫描深度">
        <el-input-number v-model="editableIntent.traversal_max_depth" :min="1" :max="10" />
      </el-form-item>
      <el-form-item label="最大页面">
        <el-input-number v-model="editableIntent.traversal_max_pages" :min="1" :max="200" />
      </el-form-item>
      <el-form-item label="任务描述"><el-input type="textarea" v-model="editableIntent.task_description" /></el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="cancelIntent">取消</el-button>
      <el-button
        v-if="editableIntent.intent === 'run_case' && editableIntent.case_file"
        @click="openCaseEditor(editableIntent.case_file)"
      >
        编辑用例
      </el-button>
      <el-button type="primary" :loading="executing" @click="confirmIntent()">确认执行</el-button>
    </template>
  </el-drawer>

  <el-dialog v-model="caseEditorVisible" title="编辑测试用例 YAML" width="62%">
    <div style="margin-bottom: 8px; color: var(--muted); font-size: 12px;">
      {{ caseEditorFile }}
    </div>
    <el-input
      v-model="caseEditorContent"
      type="textarea"
      :rows="22"
      :disabled="loadingCaseEditor || savingCaseEditor"
      placeholder="请输入或修改 YAML 用例内容"
    />
    <template #footer>
      <el-button @click="caseEditorVisible = false">关闭</el-button>
      <el-button :loading="savingCaseEditor" @click="saveCaseContent(false)">保存</el-button>
      <el-button type="primary" :loading="savingCaseEditor || executing" @click="saveCaseContent(true)">
        保存并执行
      </el-button>
    </template>
  </el-dialog>

  <el-dialog v-model="reportDetailVisible" title="测试报告详情" width="62%">
    <div v-if="selectedReport">
      <div style="margin-bottom: 8px;">
        <el-tag size="small" :type="selectedReport.status === 'success' ? 'success' : 'danger'">
          {{ selectedReport.status || "unknown" }}
        </el-tag>
        <span style="margin-left: 10px; color: var(--muted);">{{ selectedReport.name }}</span>
      </div>
      <div style="font-size: 12px; color: var(--muted); margin-bottom: 10px;">
        mode={{ selectedReport.mode }} | duration={{ selectedReport.duration_seconds || 0 }}s
      </div>
      <el-input
        :model-value="JSON.stringify(selectedReport, null, 2)"
        type="textarea"
        :rows="22"
        readonly
      />
    </div>
  </el-dialog>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { ElMessage } from "element-plus";

const activeMenu = ref("workspace");
const inputText = ref("");
const executing = ref(false);
const wsConnected = ref(false);
const deviceOnline = ref(false);
const snapshotImage = ref("");
const pageSummary = ref("");
const primaryPaths = ref([]);
const elementList = ref([]);
const logs = ref([]);
const messages = ref([]);
const intentDrawerVisible = ref(false);
const caseEditorVisible = ref(false);
const caseEditorContent = ref("");
const caseEditorFile = ref("");
const loadingCaseEditor = ref(false);
const savingCaseEditor = ref(false);
const editableIntent = ref({
  intent: "run",
  app_name: "",
  app_package: "",
  case_file: "",
  case_candidates: [],
  traversal_max_depth: 5,
  traversal_max_pages: 50,
  task_description: ""
});
const sessionId = `sess-${Date.now()}`;
let ws = null;
let snapshotTimer = null;
let snapshotInFlight = false;
const reportTasks = ref([]);
const reportDetailVisible = ref(false);
const selectedReport = ref(null);

const formattedPageSummary = computed(() => {
  const raw = (pageSummary.value || "").trim();
  if (!raw) return "暂无";
  return raw
    .replace(/^two_pane/, "双栏")
    .replace(/^single_pane/, "单栏")
    .replace("主要路径入口", "可探索入口");
});

const formattedPrimaryPaths = computed(() => {
  const labels = [];
  const seen = new Set();
  for (const item of primaryPaths.value || []) {
    const raw = String(
      item?.label || item?.text || item?.content_desc || item?.resource_id || ""
    ).trim();
    if (!raw) continue;
    if (raw.includes(":id/")) continue;
    if (/^[a-zA-Z0-9_.:-]+$/.test(raw) && !/wifi|bluetooth|setting|search/i.test(raw)) continue;
    if (/^collapse$/i.test(raw)) continue;
    const normalized = raw.replace(/\s+/g, " ").trim();
    if (seen.has(normalized)) continue;
    seen.add(normalized);
    labels.push(normalized);
    if (labels.length >= 8) break;
  }
  return labels.length ? labels.join(" / ") : "暂无";
});

function now() {
  return new Date().toLocaleTimeString();
}

function addLog(text) {
  logs.value.push(`${now()} ${text}`);
  if (logs.value.length > 500) logs.value.shift();
}

function addMessage(type, title, content) {
  messages.value.push({
    id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
    type,
    title,
    content: String(content || ""),
    time: now()
  });
}

function handleEvent(data) {
  const type = data.type;
  const content = data.content || {};
  if (type === "status") {
    addLog(`状态: ${content}`);
    addMessage("ai", "AI状态", content);
    refreshSnapshot();
  } else if (type === "intent") {
    editableIntent.value = {
      ...editableIntent.value,
      ...content,
      traversal_max_depth: content.traversal_max_depth || 5,
      traversal_max_pages: content.traversal_max_pages || 50
    };
    if (content.need_confirmation === false) {
      addLog(`意图解析完成: ${content.intent || "unknown"}（自动执行）`);
      confirmIntent(content);
      return;
    }
    executing.value = false;
    intentDrawerVisible.value = true;
    addLog(`意图解析完成: ${content.intent || "unknown"}（等待确认）`);
  } else if (type === "step_start") {
    addLog(`步骤开始: ${content.content || ""}`);
    refreshSnapshot();
  } else if (type === "step_end") {
    addLog(`步骤结束: ${content.status || ""} ${content.content || ""}`);
    refreshSnapshot();
  } else if (type === "snapshot") {
    if (content.image) {
      snapshotImage.value = content.image;
    }
  } else if (type === "anomaly") {
    addMessage("error", "异常告警", content.message || JSON.stringify(content));
  } else if (type === "scan_log") {
    const event = content.event || "scan_log";
    const payload = { ...content };
    delete payload.time;
    delete payload.event;
    delete payload.run_id;
    addLog(`[${event}] ${JSON.stringify(payload)}`);
  } else if (type === "result") {
    executing.value = false;
    stopSnapshotPolling();
    if (content.status === "need_input" && content.intent) {
      editableIntent.value = {
        ...editableIntent.value,
        ...content.intent
      };
      intentDrawerVisible.value = true;
      addLog(content.message || "需要补充参数");
      return;
    }
    if (content.mode === "generate_case" && content.case_file) {
      editableIntent.value = {
        ...editableIntent.value,
        intent: "run_case",
        case_file: content.case_file
      };
      addLog(`已生成用例: ${content.case_file}`);
      openCaseEditor(content.case_file);
      loadReports();
      return;
    }
    addMessage("ai", "执行结果", content.conclusion || content.message || JSON.stringify(content));
    refreshSnapshot();
    loadReports();
  }
}

function connectWS() {
  ws = new WebSocket(`ws://${location.host}/ws/chat`);
  ws.onopen = () => {
    wsConnected.value = true;
    addLog("WebSocket 已连接");
  };
  ws.onclose = () => {
    wsConnected.value = false;
    setTimeout(connectWS, 3000);
  };
  ws.onmessage = (e) => {
    handleEvent(JSON.parse(e.data));
  };
}

async function refreshSnapshot(force = false) {
  if (snapshotInFlight && !force) return;
  snapshotInFlight = true;
  try {
    const res = await fetch(`/api/device/snapshot?t=${Date.now()}`, { cache: "no-store" });
    const data = await res.json();
    if (data.status === "error") {
      deviceOnline.value = false;
      addLog(data.message || "设备不可用");
      return;
    }
    deviceOnline.value = true;
    snapshotImage.value = data.screen.image_base64 || "";
    pageSummary.value = data.understanding?.summary || "";
    primaryPaths.value = data.understanding?.primary_paths || [];
    elementList.value = data.understanding?.elements || [];
  } catch (e) {
    deviceOnline.value = false;
    addLog(`刷新失败: ${e}`);
  } finally {
    snapshotInFlight = false;
  }
}

async function parseIntent() {
  const text = inputText.value.trim();
  if (!text) return;
  addMessage("user", "用户指令", text);
  inputText.value = "";
  if (wsConnected.value && ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "parse", message: text }));
    return;
  }
  const res = await fetch("/api/parse", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: text, session_id: sessionId })
  });
  const data = await res.json();
  editableIntent.value = {
    ...editableIntent.value,
    ...(data.intent || {})
  };
  intentDrawerVisible.value = true;
}

function cancelIntent() {
  intentDrawerVisible.value = false;
  executing.value = false;
  stopSnapshotPolling();
  if (wsConnected.value && ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "confirm", confirmed: false }));
  }
}

async function confirmIntent(intentFromEvent = null) {
  const intent = intentFromEvent || { ...editableIntent.value };
  intentDrawerVisible.value = false;
  executing.value = true;
  startSnapshotPolling();
  if (wsConnected.value && ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "confirm", intent, confirmed: true }));
    return;
  }
  const res = await fetch("/api/confirm", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, intent, confirmed: true })
  });
  const data = await res.json();
  handleEvent({ type: "result", content: data.data || data });
}

async function sendDeviceKey(key) {
  try {
    await fetch("/api/device/key", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key })
    });
    addLog(`设备按键: ${key}`);
    setTimeout(() => refreshSnapshot(true), 350);
  } catch (e) {
    ElMessage.error(`按键失败: ${e}`);
  }
}

async function openCaseEditor(caseFile) {
  if (!caseFile) return;
  loadingCaseEditor.value = true;
  try {
    const res = await fetch(`/api/cases/content?case_file=${encodeURIComponent(caseFile)}`, {
      cache: "no-store"
    });
    const data = await res.json();
    if (data.status !== "success") {
      ElMessage.error(data.message || "读取用例失败");
      return;
    }
    caseEditorFile.value = data.case_file || caseFile;
    caseEditorContent.value = data.content || "";
    caseEditorVisible.value = true;
  } catch (e) {
    ElMessage.error(`读取用例失败: ${e}`);
  } finally {
    loadingCaseEditor.value = false;
  }
}

async function saveCaseContent(runAfterSave = false) {
  if (!caseEditorFile.value) return;
  savingCaseEditor.value = true;
  try {
    const res = await fetch("/api/cases/content", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        case_file: caseEditorFile.value,
        content: caseEditorContent.value
      })
    });
    const data = await res.json();
    if (data.status !== "success") {
      ElMessage.error(data.message || "保存失败");
      return;
    }
    ElMessage.success("用例已保存");
    editableIntent.value = {
      ...editableIntent.value,
      intent: "run_case",
      case_file: data.case_file || caseEditorFile.value
    };
    caseEditorVisible.value = false;
    if (runAfterSave) confirmIntent();
  } catch (e) {
    ElMessage.error(`保存失败: ${e}`);
  } finally {
    savingCaseEditor.value = false;
  }
}

async function loadReports() {
  try {
    const res = await fetch(`/api/reports/list?t=${Date.now()}`, { cache: "no-store" });
    const data = await res.json();
    if (data.status === "success") {
      reportTasks.value = data.items || [];
    }
  } catch (e) {
    addLog(`加载报告失败: ${e}`);
  }
}

async function openReportDetail(row) {
  if (!row?.report_path) return;
  try {
    const res = await fetch(
      `/api/reports/content?report_path=${encodeURIComponent(row.report_path)}`,
      { cache: "no-store" }
    );
    const data = await res.json();
    if (data.status !== "success") {
      ElMessage.error(data.message || "读取报告失败");
      return;
    }
    selectedReport.value = data.report || null;
    reportDetailVisible.value = true;
  } catch (e) {
    ElMessage.error(`读取报告失败: ${e}`);
  }
}

function openAllureReport() {
  window.open("/static/allure/index.html", "_blank");
}

function startSnapshotPolling() {
  if (snapshotTimer) return;
  snapshotTimer = window.setInterval(() => {
    if (executing.value) refreshSnapshot();
  }, 2500);
}

function stopSnapshotPolling() {
  if (!snapshotTimer) return;
  clearInterval(snapshotTimer);
  snapshotTimer = null;
}

onMounted(() => {
  connectWS();
  refreshSnapshot(true);
  loadReports();
});

onBeforeUnmount(() => {
  stopSnapshotPolling();
});
</script>


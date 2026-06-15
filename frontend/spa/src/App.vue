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
        <div class="header-tags">
          <el-tag :type="deviceOnline ? 'success' : 'danger'" size="small">
            {{ deviceOnline ? "设备在线" : "设备离线" }}
          </el-tag>
          <el-tag :type="wsConnected ? 'success' : 'info'" size="small" style="margin-left: 6px">
            {{ wsConnected ? "WS在线" : "HTTP模式" }}
          </el-tag>
        </div>
      </el-header>

      <!-- ═══════════ 工作台 ═══════════ -->
      <el-main class="main-content workspace-main" v-if="activeMenu === 'workspace'">
        <div class="workspace-grid">
          <!-- 设备投屏 -->
          <section class="panel panel-device">
            <div class="panel-header">
              <h3 class="panel-title">设备投屏</h3>
              <el-button size="small" @click="refreshSnapshot(true)" :disabled="!deviceOnline">刷新</el-button>
            </div>
            <div class="preview" ref="previewRef">
              <!-- 设备离线遮罩 -->
              <div v-if="!deviceOnline" class="device-offline-mask">
                <div class="device-offline-content">
                  <div class="device-offline-icon">📱</div>
                  <div class="device-offline-title">Android 设备未连接</div>
                  <div class="device-offline-desc">请检查 USB/ADB 连接后等待自动重连</div>
                  <el-tag type="warning" size="small" style="margin-top: 12px">
                    {{ devicePolling ? '正在等待设备...' : '点击重试' }}
                  </el-tag>
                </div>
              </div>
              <canvas v-if="snapshotImage" ref="previewCanvas" class="preview-canvas" />
              <img v-if="snapshotImage" ref="previewImg" :src="'data:image/png;base64,' + snapshotImage"
                   class="preview-img" @load="drawElementOverlay" />
            </div>
            <div class="device-meta">
              <el-switch v-model="showElementOverlay" size="small" active-text="元素标记"
                         @change="drawElementOverlay" style="margin-bottom: 6px" />
              <div><strong>页面语义：</strong>{{ formattedPageSummary }}</div>
            </div>
            <el-button-group style="margin-top: 10px; width: 100%">
              <el-button style="width: 25%" @click="sendDeviceKey('home')">Home</el-button>
              <el-button style="width: 25%" @click="sendDeviceKey('back')">Back</el-button>
              <el-button style="width: 25%" @click="sendDeviceKey('recent')">Recent</el-button>
              <el-button style="width: 25%" @click="sendDeviceKey('power')">Power</el-button>
            </el-button-group>
          </section>

          <!-- AI 对话与执行 -->
          <section class="panel panel-chat">
            <div class="panel-header">
              <h3 class="panel-title">AI 对话与执行</h3>
              <el-tag v-if="executing" type="warning" size="small">执行中</el-tag>
            </div>
            <div class="chat-list" ref="chatListRef">
              <div v-for="m in messages" :key="m.id" class="bubble" :class="m.type">
                <div class="bubble-title">{{ m.title }} · {{ m.time }}</div>
                <div class="bubble-content">{{ m.content }}</div>
              </div>
              <!-- 流式 token 实时显示 -->
              <div v-if="streamingToken" class="bubble ai streaming">
                <div class="bubble-title">AI 思考中 · {{ now() }}</div>
                <div class="bubble-content">{{ streamingToken }}</div>
              </div>
              <!-- 当前工具执行状态 -->
              <div v-if="currentTool" class="tool-status">
                <el-icon class="is-loading"><span>⚙</span></el-icon>
                {{ currentTool }}
              </div>
            </div>
            <el-input v-model="inputText" type="textarea" :rows="3"
                      placeholder="输入测试指令，如: 检查 Settings 的 WLAN 开关是否正常" />
            <div style="margin-top: 8px; text-align: right">
              <el-button type="primary" :loading="executing" @click="startRun">
                开始执行
              </el-button>
            </div>
          </section>

          <!-- 实时日志 -->
          <section class="panel panel-status">
            <div class="panel-header">
              <h3 class="panel-title">实时日志</h3>
              <el-button size="small" @click="logs = []">清空</el-button>
            </div>
            <div class="logs status-logs">
              <div v-for="(log, idx) in logs" :key="idx" class="log-row"
                   :class="{ 'log-warn': log.includes('异常') || log.includes('FAIL'),
                             'log-ok': log.includes('PASS') || log.includes('成功') }">
                {{ log }}
              </div>
            </div>
          </section>
        </div>
      </el-main>

      <!-- ═══════════ 报告中心 ═══════════ -->
      <el-main class="main-content" v-else>
        <section class="panel">
          <div class="panel-header">
            <h3 class="panel-title">测试报告中心</h3>
            <el-button size="small" @click="loadReports">刷新</el-button>
          </div>
          <el-table :data="reportTasks" border stripe empty-text="暂无报告" @row-click="openReportDetail" row-style="cursor:pointer">
            <el-table-column prop="created_at" label="执行时间" min-width="160">
              <template #default="{ row }">{{ (row.created_at || '').replace('T', ' ').substring(0, 19) }}</template>
            </el-table-column>
            <el-table-column prop="user_request" label="测试用例" min-width="200" show-overflow-tooltip />
            <el-table-column prop="total_steps" label="步骤数" width="80" />
            <el-table-column prop="duration_seconds" label="耗时(s)" width="90" />
            <el-table-column prop="status" label="结果" width="80">
              <template #default="{ row }">
                <el-tag :type="row.status === 'success' ? 'success' : 'danger'" size="small">
                  {{ row.status === 'success' ? '通过' : '失败' }}
                </el-tag>
              </template>
            </el-table-column>
          </el-table>
        </section>
      </el-main>
    </el-container>
  </el-container>

  <!-- ═══════════ 测试计划审阅对话框 ═══════════ -->
  <el-dialog v-model="planReviewVisible" width="680px" :close-on-click-modal="false"
             :close-on-press-escape="false" class="plan-review-dialog">
    <template #header>
      <div class="pr-title">
        <span class="pr-title-icon">📋</span>
        <span>测试计划确认</span>
        <el-tag size="small" type="info" round>{{ planReviewSteps.length }} 个步骤</el-tag>
      </div>
    </template>

    <div class="pr-body">
      <div class="pr-section">
        <div class="pr-section-label">执行步骤</div>
        <div class="pr-step-list">
          <div v-for="(step, idx) in planReviewSteps" :key="idx" class="pr-step-card">
            <div class="pr-step-num">{{ idx + 1 }}</div>
            <div class="pr-step-body">
              <div class="pr-step-row">
                <el-select v-model="step.action_type" size="small" class="pr-type-select"
                           @change="onStepTypeChange(idx)">
                  <el-option v-for="t in stepTypes" :key="t" :label="t" :value="t" />
                </el-select>
                <el-input v-model="step.target" size="small" placeholder="操作目标"
                          class="pr-target-input" />
                <el-button class="pr-remove-btn" size="small" type="danger" text circle
                           @click="removePlanStep(idx)" :disabled="planReviewSteps.length <= 1">
                  <span style="font-size:16px">×</span>
                </el-button>
              </div>
              <el-input v-model="step.intent" size="small" placeholder="步骤描述（意图）"
                        class="pr-intent-input" />
            </div>
          </div>
        </div>
        <el-button class="pr-add-btn" @click="addPlanStep">+ 添加步骤</el-button>
      </div>

      <div class="pr-section">
        <div class="pr-section-label">验证条件</div>
        <el-input v-model="planReviewVerification" type="textarea" :rows="3"
                  placeholder="每行一条验证条件" class="pr-verify-input" />
      </div>
    </div>

    <template #footer>
      <div class="pr-footer">
        <el-button size="large" @click="confirmPlan('cancel')"
                   :disabled="planReviewSubmitting">取消</el-button>
        <el-button size="large" type="primary" @click="confirmPlan('confirm')"
                   :disabled="planReviewSubmitting" :loading="planReviewSubmitting">
          确认并开始执行
        </el-button>
      </div>
    </template>
  </el-dialog>

  <!-- ═══════════ 元素身份确认对话框 ═══════════ -->
  <el-dialog v-model="identityDialogVisible" title="确认元素映射" width="520px"
             :close-on-click-modal="false" :close-on-press-escape="false">
    <div class="identity-dialog-msg">
      以下元素存在多个匹配项，LLM 推理选择了其中一个。确认写入知识库？
    </div>
    <div class="identity-list">
      <div v-for="(item, idx) in identityPending" :key="idx" class="identity-row">
        <el-checkbox v-model="item._confirmed" :label="item.target" size="small">
          <span class="id-alias">{{ item.target }}</span>
        </el-checkbox>
        <span class="id-detail">
          → rid={{ item.resource_id }} class={{ item.class_name }} role={{ item.role }}
          ({{ item.candidates_count }} 候选)
        </span>
      </div>
    </div>
    <template #footer>
      <el-button @click="identityDialogVisible = false">跳过</el-button>
      <el-button type="primary" @click="confirmIdentities">确认选中项</el-button>
    </template>
  </el-dialog>

  <!-- ═══════════ 人工确认对话框 ═══════════ -->
  <el-dialog v-model="humanDialogVisible" title="需要人工确认" width="420px"
             :close-on-click-modal="false" :close-on-press-escape="false">
    <div class="human-question">{{ humanQuestion }}</div>
    <div style="margin-top: 12px; color: var(--muted); font-size: 13px;">
      步骤: {{ humanStep }} | 操作: {{ humanAction }}
    </div>
    <template #footer>
      <el-button @click="sendHumanDecision('跳过此步')" :disabled="humanDeciding">跳过此步</el-button>
      <el-button type="danger" @click="sendHumanDecision('终止测试')" :disabled="humanDeciding">终止测试</el-button>
      <el-button type="primary" @click="sendHumanDecision('允许执行')" :disabled="humanDeciding" :loading="humanDeciding">
        允许执行
      </el-button>
    </template>
  </el-dialog>

  <!-- ═══════════ 用例编辑器 ═══════════ -->
  <el-dialog v-model="caseEditorVisible" title="测试用例 YAML" width="62%">
    <div style="margin-bottom: 8px; color: var(--muted); font-size: 12px;">
      {{ caseEditorFile }}
    </div>
    <el-input v-model="caseEditorContent" type="textarea" :rows="22"
              :disabled="loadingCaseEditor || savingCaseEditor" />
    <template #footer>
      <el-button @click="caseEditorVisible = false">关闭</el-button>
      <el-button :loading="savingCaseEditor" @click="saveCaseContent(false)">保存</el-button>
      <el-button type="primary" :loading="savingCaseEditor" @click="saveCaseContent(true)">
        保存并执行
      </el-button>
    </template>
  </el-dialog>

  <!-- ═══════════ 报告详情 ═══════════ -->
  <el-dialog v-model="reportDetailVisible" title="测试报告详情" width="68%" top="3vh">
    <div v-if="selectedReport" class="report-detail">
      <!-- 头部摘要 -->
      <div class="report-header">
        <div class="report-title">{{ selectedReport.user_request || '测试报告' }}</div>
        <div class="report-meta">
          <el-tag :type="selectedReport.status === 'success' ? 'success' : 'danger'" size="default">
            {{ selectedReport.status === 'success' ? '✅ 通过' : '❌ 失败' }}
          </el-tag>
          <span>总耗时 {{ selectedReport.duration_seconds || 0 }}s</span>
          <span>{{ (selectedReport.created_at || '').replace('T', ' ').substring(0, 19) }}</span>
        </div>
        <div class="report-summary">
          <div class="stat"><b>{{ selectedReport.total_steps }}</b> 总步骤</div>
          <div class="stat pass"><b>{{ selectedReport.pass_count }}</b> 通过</div>
          <div class="stat fail"><b>{{ selectedReport.fail_count }}</b> 失败</div>
          <div class="stat"><b>{{ selectedReport.total_steps ? Math.round(selectedReport.pass_count / selectedReport.total_steps * 100) : 0 }}%</b> 通过率</div>
        </div>
      </div>
      <!-- 步骤时间线 -->
      <div class="report-steps">
        <div class="step-card" v-for="s in (selectedReport.steps || [])" :key="s.index"
             :class="'step-' + (s.status || 'unknown')">
          <div class="step-icon">
            <span v-if="s.status === 'success'">✅</span>
            <span v-else-if="s.status === 'fail'">❌</span>
            <span v-else>⚠️</span>
          </div>
          <div class="step-body">
            <div class="step-title">
              <b>步骤 {{ s.index }}:</b> {{ s.intent || '—' }}
              <span class="step-time">{{ s.started_at ? s.started_at.replace('T', ' ').substring(10, 19) : '' }}</span>
            </div>
            <div class="step-action">操作: <code>{{ s.action_type || '—' }}</code> → {{ s.target || '—' }}</div>
            <div v-if="s.expected" class="step-expected">预期: {{ s.expected }}</div>
            <div class="step-obs" :class="'obs-' + (s.status || '')">
              <div class="obs-summary">{{ s.observation || '无观察结果' }}</div>
              <div v-if="s.raw_observation && s.raw_observation !== s.observation" style="margin-top: 4px;">
                <el-button size="small" text type="primary" @click="toggleStepDetail(s.index)">
                  {{ expandedSteps.has(s.index) ? '收起细节 ▲' : '展开细节 ▼' }}
                </el-button>
                <div v-if="expandedSteps.has(s.index)" class="obs-detail">
                  <pre>{{ s.raw_observation }}</pre>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
      <!-- 结论 -->
      <div class="report-conclusion" v-if="selectedReport.conclusion">
        <h4>📝 测试结论</h4>
        <div style="white-space: pre-wrap;">{{ selectedReport.conclusion }}</div>
      </div>
    </div>
  </el-dialog>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { ElMessage } from "element-plus";

const activeMenu = ref("workspace");
const inputText = ref("");
const executing = ref(false);
const wsConnected = ref(false);
const deviceOnline = ref(false);
const devicePolling = ref(false);
const snapshotImage = ref("");
const pageSummary = ref("");
const primaryPaths = ref([]);
const elementList = ref([]);
const showElementOverlay = ref(true);
const previewRef = ref(null);
const previewImg = ref(null);
const previewCanvas = ref(null);
const logs = ref([]);
const messages = ref([]);
const streamingToken = ref("");
const currentTool = ref("");
const chatListRef = ref(null);

// 计划审阅
const planReviewVisible = ref(false);
const planReviewSteps = ref([]);
const planReviewVerification = ref("");
const planReviewSubmitting = ref(false);
const stepTypes = ["launch_app", "click", "navigate_tab", "type_text", "swipe", "press_key", "wait", "assert"];

// 元素身份确认
const identityDialogVisible = ref(false);
const identityPending = ref([]);

// 人工确认
const humanDialogVisible = ref(false);
const humanQuestion = ref("");
const humanStep = ref(0);
const humanAction = ref("");
const humanDeciding = ref(false);
const currentThreadId = ref("");

// 用例编辑器
const caseEditorVisible = ref(false);
const caseEditorContent = ref("");
const caseEditorFile = ref("");
const loadingCaseEditor = ref(false);
const savingCaseEditor = ref(false);

// 报告
const reportTasks = ref([]);
const reportDetailVisible = ref(false);
const selectedReport = ref(null);
const expandedSteps = ref(new Set());

function toggleStepDetail(index) {
  const s = new Set(expandedSteps.value);
  if (s.has(index)) s.delete(index); else s.add(index);
  expandedSteps.value = s;
}

let ws = null;
let snapshotTimer = null;
let snapshotInFlight = false;
let streamTokenTimer = null;
let deviceStatusTimer = null;

function now() { return new Date().toLocaleTimeString(); }

const formattedPageSummary = computed(() => {
  const raw = (pageSummary.value || "").trim();
  if (!raw) return "暂无";
  return raw.replace(/^two_pane/, "双栏").replace(/^single_pane/, "单栏")
            .replace("主要路径入口", "可探索入口");
});

const formattedPrimaryPaths = computed(() => {
  const labels = [];
  const seen = new Set();
  for (const item of primaryPaths.value || []) {
    const raw = String(item?.label || item?.text || item?.content_desc || item?.resource_id || "").trim();
    if (!raw || raw.includes(":id/")) continue;
    if (/^[a-zA-Z0-9_.:-]+$/.test(raw) && !/wifi|bluetooth|setting|search/i.test(raw)) continue;
    if (/^collapse$/i.test(raw)) continue;
    const n = raw.replace(/\s+/g, " ").trim();
    if (seen.has(n)) continue;
    seen.add(n);
    labels.push(n);
    if (labels.length >= 8) break;
  }
  return labels.length ? labels.join(" / ") : "暂无";
});

function addLog(text) {
  logs.value.push(`${now()} ${text}`);
  if (logs.value.length > 500) logs.value.shift();
}

function addMessage(type, title, content) {
  messages.value.push({
    id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
    type, title,
    content: String(content || ""),
    time: now(),
  });
}

// ═══════════ WebSocket 事件处理 ═══════════

function handleEvent(data) {
  const type = data.type;
  const content = data.content || {};

  switch (type) {
    case "status":
      addLog(`状态: ${content}`);
      addMessage("ai", "AI状态", content);
      refreshSnapshot();
      break;

    case "plan_review":
      // Planner 产出计划，展示审阅编辑对话框
      flushStreamToken();
      const planData = (content.plan && content.plan.length > 0) ? content.plan : content;
      if (Array.isArray(planData) && planData.length > 0) {
        planReviewSteps.value = planData.map(s => ({
          intent: s.intent || "",
          action_type: s.action_type || "click",
          target: s.target || "",
          alternatives: s.alternatives || [],
          expected: s.expected || "",
        }));
      } else if (content.steps && Array.isArray(content.steps)) {
        planReviewSteps.value = content.steps.map(s => ({
          intent: s.intent || "",
          action_type: s.action_type || "click",
          target: s.target || "",
          alternatives: s.alternatives || [],
          expected: s.expected || "",
        }));
      }
      planReviewVerification.value = (content.verification || []).join("\n");
      planReviewVisible.value = true;
      addLog(`计划已生成: ${planReviewSteps.value.length} 个步骤，请确认`);
      break;

    case "plan_ready":
      addLog(`计划已生成: ${content.steps || "?"} 个步骤`);
      break;

    case "stream_token":
      // 流式 token：累积到 streamingToken，定时刷新
      streamingToken.value += String(content || "");
      resetStreamTimer();
      break;

    case "tool_start":
      currentTool.value = `正在执行: ${content.name || ""}`;
      addLog(`🔧 ${content.name || "tool"}`);
      break;

    case "tool_end":
      currentTool.value = "";
      if (content.name) addLog(`   ✓ ${content.name} 完成`);
      break;

    case "step_start":
      addLog(`▶ 步骤开始: ${content.content || content.step || ""}`);
      refreshSnapshot();
      break;

    case "step_end":
      addLog(`   ✓ 步骤结束: ${content.content || content.status || ""}`);
      refreshSnapshot();
      break;

    case "snapshot":
      if (content.image) snapshotImage.value = content.image;
      break;

    case "anomaly":
      addMessage("error", "异常告警", content.message || JSON.stringify(content));
      addLog(`⚠ 异常: ${content.message || content.description || ""}`);
      break;

    case "need_human_approval":
      // 人工确认
      currentThreadId.value = content.thread_id || currentThreadId.value;
      humanQuestion.value = content.question || "是否继续执行?";
      humanStep.value = content.step || 0;
      humanAction.value = content.action || "";
      humanDialogVisible.value = true;
      executing.value = false;
      addLog(`⏸ 需要人工确认: ${humanQuestion.value}`);
      break;

    case "result":
      // need_human → 触发计划审阅或人工确认
      if (content.status === "need_human" || content.interrupt) {
        const intr = content.interrupt || content;
        if (intr.type === "plan_review") {
          // 触发计划审阅
          const plan = intr.plan || [];
          planReviewSteps.value = plan.map(s => ({
            intent: s.intent || "", action_type: s.action_type || "click",
            target: s.target || "", alternatives: s.alternatives || [],
            expected: s.expected || "",
          }));
          planReviewVerification.value = (intr.verification || []).join("\n");
          planReviewVisible.value = true;
          currentThreadId.value = content.thread_id || "";
          addLog(`计划已生成: ${plan.length} 个步骤，请确认`);
        } else {
          // 人工确认
          humanQuestion.value = intr.question || "是否继续?";
          humanStep.value = intr.step || 0;
          humanAction.value = intr.action || "";
          humanDialogVisible.value = true;
          addLog("需要人工确认");
        }
        executing.value = false;
        stopSnapshotPolling();
        break;
      }
      // Level2 元素身份确认
      const pendingIds = content.pending_identities || [];
      if (content.status === "success" && pendingIds.length > 0) {
        const level2 = pendingIds.filter(p => p.level === 2);
        if (level2.length > 0) {
          identityPending.value = level2;
          identityDialogVisible.value = true;
          currentThreadId.value = content.thread_id || "";
          addLog(`发现 ${level2.length} 个待确认的元素映射`);
        }
      }
      // 最终结果
      executing.value = false;
      stopSnapshotPolling();
      flushStreamToken();
      currentTool.value = "";
      addMessage("ai", "执行结果", content.conclusion || content.message || JSON.stringify(content));
      addLog(`${content.status === "success" ? "PASS" : "FAIL"}: ${(content.conclusion || "").substring(0, 200)}`);
      refreshSnapshot();
      loadReports();
      break;

    case "error":
      addLog(`❌ 错误: ${content}`);
      executing.value = false;
      stopSnapshotPolling();
      break;

    default:
      addLog(`[${type}] ${JSON.stringify(content).substring(0, 200)}`);
  }
}

function resetStreamTimer() {
  if (streamTokenTimer) clearTimeout(streamTokenTimer);
  streamTokenTimer = setTimeout(flushStreamToken, 2000);
}

function flushStreamToken() {
  if (streamingToken.value.trim()) {
    addMessage("ai", "AI输出", streamingToken.value.trim());
    streamingToken.value = "";
  }
  if (streamTokenTimer) {
    clearTimeout(streamTokenTimer);
    streamTokenTimer = null;
  }
}

// ═══════════ WebSocket 连接 ═══════════

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
  ws.onmessage = (e) => handleEvent(JSON.parse(e.data));
}

// ═══════════ 一键执行 ═══════════

async function startRun() {
  const text = inputText.value.trim();
  if (!text) return;
  if (!deviceOnline.value) {
    ElMessage.warning("Android 设备未连接，请先连接设备");
    return;
  }
  addMessage("user", "用户指令", text);
  inputText.value = "";
  executing.value = true;
  streamingToken.value = "";
  currentTool.value = "";
  startSnapshotPolling();

  if (wsConnected.value && ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "run", message: text }));
    return;
  }
  // HTTP fallback
  try {
    const res = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
    });
    const data = await res.json();
    handleEvent({ type: "result", content: data.data || data });
  } catch (e) {
    handleEvent({ type: "error", content: String(e) });
  }
}

// ═══════════ 人工确认 ═══════════

async function sendHumanDecision(decision) {
  humanDeciding.value = true;
  executing.value = true;
  humanDialogVisible.value = false;
  addLog(`人工决定: ${decision}`);
  startSnapshotPolling();

  if (wsConnected.value && ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "human_decision", thread_id: currentThreadId.value, decision }));
    humanDeciding.value = false;
    return;
  }
  // HTTP fallback
  try {
    const res = await fetch("/api/human_decision", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ thread_id: currentThreadId.value, decision }),
    });
    const data = await res.json();
    handleEvent({ type: "result", content: data.data || data });
  } catch (e) {
    handleEvent({ type: "error", content: String(e) });
  } finally {
    humanDeciding.value = false;
  }
}

// ═══════════ 元素身份确认 ═══════════

async function confirmIdentities() {
  const confirmed = identityPending.value.filter(p => p._confirmed);
  if (confirmed.length === 0) {
    identityDialogVisible.value = false;
    return;
  }
  try {
    await fetch("/api/element_identities/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ identities: confirmed }),
    });
    addLog(`已确认 ${confirmed.length} 个元素映射`);
  } catch (e) { /* ignore */ }
  identityDialogVisible.value = false;
}

// ═══════════ 计划审阅 ═══════════

function addPlanStep() {
  planReviewSteps.value.push({
    intent: "新步骤",
    action_type: "click",
    target: "",
    alternatives: [],
    expected: "",
  });
}

function removePlanStep(idx) {
  if (planReviewSteps.value.length <= 1) return;
  planReviewSteps.value.splice(idx, 1);
}

function onStepTypeChange(idx) {
  const step = planReviewSteps.value[idx];
  if (!step) return;
  if (step.action_type === "wait" && !step.target) step.target = "1";
}

async function confirmPlan(action) {
  if (planReviewSubmitting.value) return;  // 防抖：已在提交中
  planReviewSubmitting.value = true;
  planReviewVisible.value = false;
  executing.value = true;
  startSnapshotPolling();

  // 重建完整计划
  const editedPlan = planReviewSteps.value.map((s, i) => ({
    index: i + 1,
    intent: s.intent,
    action_type: s.action_type,
    target: s.target,
    alternatives: s.alternatives || [],
    expected: s.expected || "",
  }));

  const verification = planReviewVerification.value
    .split("\n")
    .map(v => v.trim())
    .filter(v => v);

  const resumePayload = action === "cancel"
    ? { action: "cancel" }
    : { action: "confirm", plan: editedPlan, verification };

  addLog(action === "cancel" ? "计划已取消" : `计划已确认: ${editedPlan.length} 步骤`);

  if (wsConnected.value && ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "human_decision", thread_id: currentThreadId.value, decision: resumePayload }));
    planReviewSubmitting.value = false;
    return;
  }

  try {
    const res = await fetch("/api/human_decision", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ thread_id: currentThreadId.value, decision: resumePayload }),
    });
    const data = await res.json();
    handleEvent({ type: "result", content: data.data || data });
  } catch (e) {
    handleEvent({ type: "error", content: String(e) });
  } finally {
    planReviewSubmitting.value = false;
  }
}

// ═══════════ 元素覆盖绘制 ═══════════

const ROLE_COLORS = {
  switch: { stroke: '#409eff', fill: 'rgba(64,158,255,0.12)' },
  navigation_item: { stroke: '#67c23a', fill: 'rgba(103,194,58,0.10)' },
  tab: { stroke: '#e6a23c', fill: 'rgba(230,162,60,0.10)' },
  list_entry: { stroke: '#909399', fill: 'rgba(144,147,153,0.08)' },
  settings_entry: { stroke: '#909399', fill: 'rgba(144,147,153,0.08)' },
  button: { stroke: '#f56c6c', fill: 'rgba(245,108,108,0.10)' },
  text: { stroke: '#b0b0b0', fill: 'rgba(176,176,176,0.06)' },
  default: { stroke: '#409eff', fill: 'rgba(64,158,255,0.08)' },
};

function drawElementOverlay() {
  const img = previewImg.value;
  const canvas = previewCanvas.value;
  if (!img || !canvas || !img.complete) return;

  const dw = img.naturalWidth || 1080;
  const dh = img.naturalHeight || 1920;
  const scaleX = img.clientWidth / dw;
  const scaleY = img.clientHeight / dh;

  canvas.width = img.clientWidth;
  canvas.height = img.clientHeight;
  canvas.style.position = 'absolute';
  canvas.style.top = '0';
  canvas.style.left = '0';
  canvas.style.pointerEvents = 'none';

  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  if (!showElementOverlay.value) return;

  const elements = elementList.value || [];
  for (const el of elements) {
    if (!el.bounds || el.bounds.length !== 4) continue;
    let [l, t, r, b] = el.bounds;
    l *= scaleX; t *= scaleY; r *= scaleX; b *= scaleY;
    if (r - l < 2 || b - t < 2) continue;

    const role = el.role || 'default';
    const colors = ROLE_COLORS[role] || ROLE_COLORS.default;

    ctx.strokeStyle = colors.stroke;
    ctx.fillStyle = colors.fill;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.roundRect(l, t, r - l, b - t, 4);
    ctx.fill();
    ctx.stroke();

    // Label
    const label = (el.label || el.text || el.content_desc || '').substring(0, 12);
    if (label && (el.clickable || role === 'switch')) {
      ctx.fillStyle = colors.stroke;
      ctx.font = '11px sans-serif';
      const textW = ctx.measureText(label).width + 6;
      ctx.fillRect(l, Math.max(0, t - 18), textW, 16);
      ctx.fillStyle = '#fff';
      ctx.fillText(label, l + 3, Math.max(0, t - 18) + 12);
    }
  }
}

// ═══════════ 设备操作 ═══════════

async function refreshSnapshot(force = false) {
  if (snapshotInFlight && !force) return;
  snapshotInFlight = true;
  try {
    const res = await fetch(`/api/device/snapshot?t=${Date.now()}`, { cache: "no-store" });
    if (res.status === 503) {
      deviceOnline.value = false;
      startDevicePolling();
      return;
    }
    const data = await res.json();
    if (data.status === "error") {
      deviceOnline.value = false;
      startDevicePolling();
      return;
    }
    deviceOnline.value = true;
    stopDevicePolling();
    snapshotImage.value = (data.screen || {}).image_base64 || "";
    pageSummary.value = (data.understanding || {}).summary || "";
    primaryPaths.value = (data.understanding || {}).primary_paths || [];
    elementList.value = (data.understanding || {}).elements || [];
    setTimeout(drawElementOverlay, 100);
  } catch (e) {
    deviceOnline.value = false;
    startDevicePolling();
  } finally {
    snapshotInFlight = false;
  }
}

async function sendDeviceKey(key) {
  try {
    await fetch("/api/device/key", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key }),
    });
    addLog(`设备按键: ${key}`);
    setTimeout(() => refreshSnapshot(true), 350);
  } catch (e) {
    ElMessage.error(`按键失败: ${e}`);
  }
}

// ═══════════ 用例编辑 ═══════════

async function openCaseEditor(caseFile) {
  if (!caseFile) return;
  loadingCaseEditor.value = true;
  try {
    const res = await fetch(`/api/cases/content?case_file=${encodeURIComponent(caseFile)}`, { cache: "no-store" });
    const data = await res.json();
    if (data.status !== "success") { ElMessage.error(data.message || "读取失败"); return; }
    caseEditorFile.value = data.case_file || caseFile;
    caseEditorContent.value = data.content || "";
    caseEditorVisible.value = true;
  } catch (e) {
    ElMessage.error(`读取失败: ${e}`);
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
      body: JSON.stringify({ case_file: caseEditorFile.value, content: caseEditorContent.value }),
    });
    const data = await res.json();
    if (data.status !== "success") { ElMessage.error(data.message || "保存失败"); return; }
    ElMessage.success("用例已保存");
    caseEditorVisible.value = false;
    if (runAfterSave) {
      inputText.value = `执行 ${data.case_file || caseEditorFile.value}`;
      startRun();
    }
  } catch (e) {
    ElMessage.error(`保存失败: ${e}`);
  } finally {
    savingCaseEditor.value = false;
  }
}

// ═══════════ 报告 ═══════════

async function loadReports() {
  try {
    const res = await fetch(`/api/reports/list?t=${Date.now()}`, { cache: "no-store" });
    const data = await res.json();
    if (data.status === "success") reportTasks.value = data.items || [];
  } catch (e) { /* ignore */ }
}

async function openReportDetail(row) {
  if (!row?.id) return;
  try {
    const res = await fetch(`/api/reports/${encodeURIComponent(row.id)}?t=${Date.now()}`, { cache: "no-store" });
    const data = await res.json();
    if (data.status !== "success") { ElMessage.error(data.message || "读取失败"); return; }
    selectedReport.value = data.report || null;
    reportDetailVisible.value = true;
  } catch (e) {
    ElMessage.error(`读取失败: ${e}`);
  }
}

function startSnapshotPolling() {
  if (snapshotTimer) return;
  snapshotTimer = window.setInterval(() => { if (executing.value) refreshSnapshot(); }, 2500);
}

function stopSnapshotPolling() {
  if (!snapshotTimer) return;
  clearInterval(snapshotTimer);
  snapshotTimer = null;
}

// ═══════════ 设备状态轮询 ═══════════

async function checkDeviceStatus() {
  try {
    const res = await fetch("/api/device/status", { cache: "no-store" });
    const data = await res.json();
    const wasOffline = !deviceOnline.value;
    deviceOnline.value = !!data.connected;
    if (data.connected && wasOffline) {
      // 设备刚上线，加载截图
      addLog("Android 设备已连接");
      refreshSnapshot(true);
      stopDevicePolling();
    } else if (!data.connected) {
      startDevicePolling();
    }
  } catch (e) {
    deviceOnline.value = false;
    startDevicePolling();
  }
}

function startDevicePolling() {
  if (deviceStatusTimer) return;
  devicePolling.value = true;
  deviceStatusTimer = window.setInterval(checkDeviceStatus, 3000);
}

function stopDevicePolling() {
  if (!deviceStatusTimer) return;
  clearInterval(deviceStatusTimer);
  deviceStatusTimer = null;
  devicePolling.value = false;
}

onMounted(() => {
  connectWS();
  checkDeviceStatus();
  loadReports();
  window.addEventListener('resize', drawElementOverlay);
});

onBeforeUnmount(() => {
  stopSnapshotPolling();
  stopDevicePolling();
  flushStreamToken();
  window.removeEventListener('resize', drawElementOverlay);
});
</script>

<style scoped>
/* 复用原有样式，新增流式/工具状态样式 */
.tool-status {
  padding: 6px 12px;
  margin: 4px 0;
  font-size: 13px;
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  border-radius: 6px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.bubble.streaming .bubble-content {
  border-left: 3px solid var(--el-color-primary);
  padding-left: 10px;
  animation: pulse 1.5s infinite;
}
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.6; }
}
.log-warn { color: var(--el-color-warning); }
.log-ok { color: var(--el-color-success); }
.human-question {
  font-size: 15px;
  font-weight: 600;
  color: var(--el-color-danger);
  line-height: 1.6;
}
.header-tags {
  display: flex;
  align-items: center;
  gap: 4px;
}

/* ── Plan Review Dialog ── */
.pr-title {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 17px;
  font-weight: 600;
  color: #1a1a2e;
}
.pr-title-icon { font-size: 20px; }
.pr-body { padding: 4px 0; }
.pr-section { margin-bottom: 20px; }
.pr-section-label {
  font-size: 13px;
  font-weight: 600;
  color: #6b7280;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 10px;
}
.pr-step-list { display: flex; flex-direction: column; gap: 10px; }
.pr-step-card {
  display: flex;
  gap: 12px;
  padding: 14px 14px 14px 10px;
  background: #f8f9fb;
  border-radius: 10px;
  border: 1px solid #e8ecf1;
  transition: border-color 0.2s;
}
.pr-step-card:hover { border-color: #c0c8d4; }
.pr-step-num {
  width: 28px; height: 28px;
  display: flex; align-items: center; justify-content: center;
  background: #e0e5ec; color: #4a5568;
  border-radius: 50%; font-size: 13px; font-weight: 700;
  flex-shrink: 0; margin-top: 2px;
}
.pr-step-body { flex: 1; display: flex; flex-direction: column; gap: 7px; }
.pr-step-row { display: flex; gap: 8px; align-items: center; }
.pr-type-select { width: 130px; flex-shrink: 0; }
.pr-target-input { width: 140px; flex-shrink: 0; }
.pr-intent-input { width: 100%; }
.pr-remove-btn { flex-shrink: 0; }
.pr-add-btn {
  margin-top: 8px; width: 100%; border: 1px dashed #c0c8d4;
  color: #6b7280; background: transparent; padding: 8px;
  border-radius: 8px; font-size: 13px; cursor: pointer;
  transition: all 0.2s;
}
.pr-add-btn:hover { border-color: #409eff; color: #409eff; background: #f0f5ff; }
.pr-verify-input { margin-top: 2px; }
.pr-footer { display: flex; justify-content: flex-end; gap: 10px; }

/* ── Device Preview Overlay ── */
.preview { position: relative; overflow: hidden; border-radius: 8px; background: #1a1a2e; }
.preview-img { width: 100%; display: block; }
.preview-canvas { position: absolute; top: 0; left: 0; width: 100%; height: 100%; }

/* ── Device Offline Mask ── */
.device-offline-mask {
  position: absolute; top: 0; left: 0; right: 0; bottom: 0;
  background: rgba(26, 26, 46, 0.92);
  display: flex; align-items: center; justify-content: center;
  z-index: 10; border-radius: 8px;
}
.device-offline-content { text-align: center; color: #e0e0e0; }
.device-offline-icon { font-size: 48px; margin-bottom: 12px; }
.device-offline-title { font-size: 18px; font-weight: 600; margin-bottom: 6px; color: #fff; }
.device-offline-desc { font-size: 13px; color: #aaa; }

/* ── Report Detail ── */
.report-detail { max-height: 70vh; overflow-y: auto; }
.report-header { margin-bottom: 20px; padding-bottom: 16px; border-bottom: 1px solid #eee; }
.report-title { font-size: 18px; font-weight: 700; margin-bottom: 10px; color: #1a1a2e; }
.report-meta { display: flex; gap: 16px; align-items: center; font-size: 13px; color: #666; margin-bottom: 14px; }
.report-summary { display: flex; gap: 24px; }
.report-summary .stat { font-size: 14px; color: #555; }
.report-summary .stat b { font-size: 20px; margin-right: 4px; }
.report-summary .stat.pass b { color: #67c23a; }
.report-summary .stat.fail b { color: #f56c6c; }
.report-steps { display: flex; flex-direction: column; gap: 12px; }
.step-card { display: flex; gap: 14px; padding: 14px; border-radius: 8px; background: #fafafa; border-left: 4px solid #ddd; }
.step-card.step-success { border-left-color: #67c23a; background: #f0f9eb; }
.step-card.step-fail { border-left-color: #f56c6c; background: #fef0f0; }
.step-icon { font-size: 24px; flex-shrink: 0; padding-top: 2px; }
.step-body { flex: 1; min-width: 0; }
.step-title { font-size: 15px; margin-bottom: 6px; }
.step-title .step-time { font-size: 12px; color: #999; margin-left: 10px; }
.step-action { font-size: 13px; color: #555; margin-bottom: 4px; }
.step-action code { background: #e8e8e8; padding: 1px 6px; border-radius: 3px; font-size: 12px; }
.step-expected { font-size: 12px; color: #888; margin-bottom: 6px; }
.step-obs { font-size: 13px; padding: 8px 10px; border-radius: 4px; background: #fff; white-space: pre-wrap; word-break: break-all; }
.step-obs.obs-success { border-left: 3px solid #67c23a; }
.step-obs.obs-fail { border-left: 3px solid #f56c6c; }
.obs-summary { color: #333; }
.obs-detail { margin-top: 8px; }
.obs-detail pre { background: #f0f0f0; padding: 10px; border-radius: 4px; font-size: 12px; white-space: pre-wrap; word-break: break-all; max-height: 300px; overflow-y: auto; margin: 0; }
.report-conclusion { margin-top: 20px; padding: 16px; background: #f5f7fa; border-radius: 8px; }
.report-conclusion h4 { margin: 0 0 8px 0; font-size: 16px; }
</style>

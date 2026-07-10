<template>
  <el-container class="layout-root">
    <el-aside width="220px" class="side-nav">
      <div class="brand">
        <span class="brand-icon">◇</span>
        AI Workbench
      </div>
      <div class="menu-card">
        <el-menu :default-active="activeMenu" @select="activeMenu = $event">
        <el-menu-item index="workspace">
          <el-icon><i class="nav-icon">💬</i></el-icon>
          <span>工作台</span>
        </el-menu-item>
        <el-menu-item index="reports">
          <el-icon><i class="nav-icon">📊</i></el-icon>
          <span>报告中心</span>
        </el-menu-item>
        <el-menu-item index="apps">
          <el-icon><i class="nav-icon">📱</i></el-icon>
          <span>APP 管理</span>
        </el-menu-item>
        <el-menu-item index="knowledge">
          <el-icon><i class="nav-icon">📚</i></el-icon>
          <span>知识库</span>
        </el-menu-item>
        <el-menu-item index="settings">
          <el-icon><i class="nav-icon">⚙️</i></el-icon>
          <span>设置</span>
        </el-menu-item>
        </el-menu>
      </div>

      <!-- 侧边栏底部: 设备信息卡片 -->
      <div class="side-device-card">
        <template v-if="deviceOnline && deviceInfo">
          <div class="side-device-header">
            <span class="side-device-dot online"></span>
            <span class="side-device-title">{{ deviceInfo.brand }} {{ deviceInfo.model }}</span>
          </div>
          <div class="side-device-body">
            <div class="side-device-row"><span>SN</span><span>{{ deviceInfo.serial }}</span></div>
            <div class="side-device-row"><span>屏幕</span><span>{{ deviceInfo.screen }}</span></div>
            <div class="side-device-row"><span>Android</span><span>{{ deviceInfo.android_version }}</span></div>
          </div>
        </template>
        <template v-else>
          <div class="side-device-header">
            <span class="side-device-dot offline"></span>
            <span class="side-device-title">设备离线</span>
          </div>
          <div class="side-device-body">
            <div class="side-device-hint">请检查 USB / ADB 连接</div>
            <button class="side-device-reconnect" @click="reconnectDevice">重新连接</button>
          </div>
        </template>
      </div>

      <DeviceFloat ref="deviceFloatRef"
        :deviceOnline="deviceOnline"
        :snapshotImage="snapshotImage"
        :pageSummary="pageSummary"
        :elementList="elementList"
        @refresh="refreshSnapshot"
        @send-key="sendDeviceKey"
      />
    </el-aside>

    <el-container>
      <el-header class="topbar">
        <el-breadcrumb separator="/">
          <el-breadcrumb-item>AI 测试平台</el-breadcrumb-item>
          <el-breadcrumb-item>{{ activeMenu === 'workspace' ? '工作台' : activeMenu === 'reports' ? '报告中心' : activeMenu === 'apps' ? 'APP 管理' : activeMenu === 'settings' ? '设置' : '知识库' }}</el-breadcrumb-item>
        </el-breadcrumb>
        <div class="header-tags">
          <el-tag :type="wsConnected ? 'success' : 'info'" size="small" effect="light" round>
            {{ wsConnected ? "WS在线" : "HTTP模式" }}
          </el-tag>
        </div>
      </el-header>

      <!-- ═══════════ 工作台 ═══════════ -->
      <el-main class="main-content workspace-main" v-show="activeMenu === 'workspace'">
        <WorkspacePanel ref="workspaceRef" :executing="executing" @run="startRun" />
      </el-main>

      <!-- ═══════════ 报告中心 ═══════════ -->
      <el-main class="main-content" v-show="activeMenu === 'reports'">
        <section class="panel">
          <div class="panel-header">
            <h3 class="panel-title">测试报告中心</h3>
            <el-button size="small" @click="loadReports">刷新</el-button>
          </div>
          <el-table :data="reportTasks" empty-text="暂无报告" @row-click="openReportDetail" row-style="cursor:pointer">
            <el-table-column prop="created_at" label="执行时间" min-width="160">
              <template #default="{ row }">{{ (row.created_at || '').replace('T', ' ').substring(0, 19) }}</template>
            </el-table-column>
            <el-table-column prop="user_request" label="测试用例" min-width="200" show-overflow-tooltip />
            <el-table-column prop="total_steps" label="步骤数" width="80" />
            <el-table-column prop="duration_seconds" label="耗时(s)" width="90" />
            <el-table-column prop="llm_call_count" label="LLM调用" width="90" />
            <el-table-column prop="tool_call_400_count" label="400次数" width="90" />
            <el-table-column label="400占比" width="100">
              <template #default="{ row }">
                {{ (((Number(row.tool_call_400_rate) || 0) * 100)).toFixed(2) }}%
              </template>
            </el-table-column>
            <el-table-column label="执行状态" width="100">
              <template #default="{ row }">
                <el-tag :type="execStatusType(row.execution_status)" size="small">
                  {{ execStatusLabel(row.execution_status) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="测试结论" width="90">
              <template #default="{ row }">
                <el-tag :type="verdictType(row.test_verdict)" size="small">
                  {{ verdictLabel(row.test_verdict) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="90" fixed="right">
              <template #default="{ row }">
                <el-button size="small" text type="danger" @click.stop="deleteReport(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
        </section>
      </el-main>

      <!-- ═══════════ 知识库 ═══════════ -->
      <el-main class="main-content" v-show="activeMenu === 'knowledge'">
        <KnowledgePanel
          :list="kbList" :kbCount="kbCount" :types="kbTypes"
          @refresh="loadKbList" @add="openKbDialog(null)"
          @search="({ query, type, pkg }) => { kbSearchQuery = query; kbFilterType = type; kbFilterPackage = pkg; searchKb() }"
          @deleteByFilter="({ type, pkg }) => { kbFilterType = type; kbFilterPackage = pkg; deleteKbByFilter() }"
          @detail="openKbDetail"
        />
      </el-main>

      <!-- ═══════════ APP 管理 ═══════════ -->
      <el-main class="main-content" v-show="activeMenu === 'apps'">
        <section class="panel">
          <div class="panel-header">
            <h3 class="panel-title">APP 管理</h3>
            <div style="display:flex;gap:8px">
              <el-button size="small" @click="loadApps">刷新</el-button>
              <el-button size="small" type="primary" @click="openAppDialog(null)">+ 添加</el-button>
            </div>
          </div>
          <el-table :data="appList" empty-text="暂无应用，点击右上角添加">
            <el-table-column prop="name" label="应用名称" min-width="120" />
            <el-table-column prop="package" label="包名" min-width="220" show-overflow-tooltip />
            <el-table-column label="触发关键词" min-width="200">
              <template #default="{ row }">
                <el-tag v-for="kw in (row.keywords || [])" :key="kw"
                        size="small" style="margin:2px">{{ kw }}</el-tag>
                <span v-if="!(row.keywords && row.keywords.length)" style="color:var(--text-muted);font-size:12px">未设置</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="130" fixed="right">
              <template #default="{ row }">
                <el-button size="small" text type="primary" @click.stop="openAppDialog(row)">编辑</el-button>
                <el-button size="small" text type="danger" @click.stop="deleteApp(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
        </section>
      </el-main>

      <!-- ═══════════ 设置 ═══════════ -->
      <el-main class="main-content" v-show="activeMenu === 'settings'">
        <section class="panel" v-if="configData">
          <div class="panel-header">
            <h3 class="panel-title">系统设置</h3>
            <el-button type="primary" :loading="configSaving" @click="saveConfig">保存配置</el-button>
          </div>

          <div class="settings-sections">
            <!-- LLM 配置 -->
            <div class="settings-group">
              <h4 class="settings-group-title">LLM 模型</h4>
              <el-form label-width="110px" size="default">
                <el-form-item label="Provider">
                  <el-select v-model="configData.llm_provider" style="width:100%">
                    <el-option label="OpenAI (兼容)" value="openai" />
                    <el-option label="智谱 Zhipu" value="zhipu" />
                  </el-select>
                </el-form-item>
                <el-form-item label="模型名称">
                  <el-input v-model="configData.model" placeholder="如: deepseek-v4-pro, gpt-4o" />
                </el-form-item>
                <el-form-item label="API Key">
                  <el-input v-model="configData.api_key" type="password" show-password placeholder="API Key" />
                </el-form-item>
                <el-form-item label="Base URL">
                  <el-input v-model="configData.base_url" placeholder="如: https://api.deepseek.com" />
                </el-form-item>
                <el-form-item label="多模态开关">
                  <el-switch
                    v-model="configData.vision_enabled"
                    active-text="开启"
                    inactive-text="关闭"
                  />
                </el-form-item>
              </el-form>
            </div>

            <!-- Embedding -->
            <div class="settings-group">
              <h4 class="settings-group-title">Embedding（RAG 向量化）</h4>
              <el-form label-width="110px" size="default">
                <el-form-item label="Provider">
                  <el-select v-model="configData.embedding_provider" style="width:100%">
                    <el-option label="HuggingFace (本地)" value="huggingface" />
                    <el-option label="OpenAI (兼容)" value="openai" />
                  </el-select>
                </el-form-item>
                <el-form-item label="模型名称">
                  <el-input v-model="configData.embedding_model" placeholder="如: BAAI/bge-large-zh-v1.5" />
                </el-form-item>
              </el-form>
            </div>

            <!-- 感知模式 & 安全等级 -->
            <div class="settings-group">
              <h4 class="settings-group-title">运行参数</h4>
              <el-form label-width="110px" size="default">
                <el-form-item label="感知模式">
                  <el-select v-model="configData.perception_mode" style="width:100%">
                    <el-option label="UI Tree（最快）" value="ui_tree" />
                    <el-option label="Hybrid（自动切换）" value="hybrid" />
                  </el-select>
                </el-form-item>
                <el-form-item label="安全等级">
                  <el-select v-model="configData.safety_level" style="width:100%">
                    <el-option label="Strict（严格）" value="strict" />
                    <el-option label="Relaxed（宽松）" value="relaxed" />
                  </el-select>
                </el-form-item>
              </el-form>
            </div>
          </div>
        </section>
        <section class="panel" v-else style="display:flex;align-items:center;justify-content:center;min-height:200px">
          <span style="color:var(--text-muted)">加载配置中...</span>
        </section>
      </el-main>
  
    </el-container>
  </el-container>
  
  <!-- ═══════════ 测试目标确认对话框（可编辑）═══════════ -->
  <el-dialog v-model="planReviewVisible" width="560px" :close-on-click-modal="false"
             :close-on-press-escape="false" class="plan-review-dialog">
    <template #header>
      <div class="pr-title">
        <span class="pr-title-icon">🎯</span>
        <span>测试目标确认（可编辑）</span>
      </div>
    </template>

    <div class="pr-body">
      <div class="pr-section">
        <div class="pr-section-label">目标</div>
        <el-input v-model="planReviewGoal" type="textarea" :rows="2" placeholder="测试目标" />
      </div>
      <div class="pr-section">
        <div class="pr-section-label">目标页面</div>
        <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:6px">
          <el-tag v-for="(p, i) in planReviewPages" :key="i" size="small" closable @close="planReviewPages.splice(i,1)">{{ p }}</el-tag>
        </div>
        <div style="display:flex;gap:6px">
          <el-input v-model="newPageName" size="small" placeholder="添加页面" @keyup.enter="addReviewPage" style="flex:1" />
          <el-button size="small" @click="addReviewPage">+</el-button>
        </div>
      </div>
      <div class="pr-section">
        <div class="pr-section-label">验证条件</div>
        <div v-for="(v, i) in planReviewVerifications" :key="i" style="display:flex;gap:6px;margin-bottom:4px">
          <el-input v-model="planReviewVerifications[i]" size="small" />
          <el-button size="small" type="danger" text @click="planReviewVerifications.splice(i,1)">×</el-button>
        </div>
        <el-button size="small" @click="planReviewVerifications.push('')">+ 添加验证</el-button>
      </div>
      <div class="pr-section">
        <div class="pr-section-label">导航提示</div>
        <div v-for="(h, i) in planReviewHints" :key="i" style="display:flex;gap:6px;margin-bottom:4px">
          <el-input v-model="planReviewHints[i]" size="small" />
          <el-button size="small" type="danger" text @click="planReviewHints.splice(i,1)">×</el-button>
        </div>
        <el-button size="small" @click="planReviewHints.push('')">+ 添加提示</el-button>
      </div>
    </div>

    <template #footer>
      <div class="pr-footer">
        <el-button size="large" @click="confirmPlan('cancel')">取消</el-button>
        <el-button size="large" type="primary" @click="confirmPlan('confirm')">
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
    <div style="margin-top: 12px; color: var(--text-muted); font-size: 13px;">
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

  <!-- ═══════════ 报告详情 ═══════════ -->
  <el-dialog v-model="reportDetailVisible" title="测试报告详情" width="720px" top="3vh">
    <ReportDetail :report="selectedReport" />
  </el-dialog>

  <!-- ═══════════ 知识库新增/编辑对话框 ═══════════ -->
  <el-dialog v-model="kbDialogVisible" :title="kbDialogMode === 'add' ? '新增知识' : '编辑知识'" width="560px" :close-on-click-modal="false">
    <el-form :model="kbForm" label-width="90px" style="padding-right:12px">
      <el-form-item label="应用包名" required>
        <el-input v-model="kbForm.app_package" :placeholder="kbForm.knowledge_type === 'curated_rule' ? '留空表示全局知识' : '如: com.android.settings'" />
      </el-form-item>
      <el-form-item label="知识类型" required>
        <el-select v-model="kbForm.knowledge_type" style="width:100%" placeholder="请选择">
          <el-option v-for="t in kbTypes" :key="t.value" :label="t.label" :value="t.value" />
        </el-select>
      </el-form-item>
      <el-form-item label="内容" required>
        <el-input v-model="kbForm.content" type="textarea" :rows="5"
                  placeholder="知识内容，支持语义检索" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="kbDialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="kbSaving" @click="saveKb">{{ kbDialogMode === 'add' ? '保存' : '更新' }}</el-button>
    </template>
  </el-dialog>

  <!-- ═══════════ 知识库详情对话框 ═══════════ -->
  <el-dialog v-model="kbDetailVisible" title="知识详情" width="600px">
    <div v-if="kbDetailRow" class="kb-detail">
      <div class="kb-detail-row">
        <span class="kb-detail-label">知识类型</span>
        <el-tag :type="kbTypeColor(kbDetailRow.metadata && kbDetailRow.metadata.knowledge_type)">
          {{ kbTypeLabel(kbDetailRow.metadata && kbDetailRow.metadata.knowledge_type) }}
        </el-tag>
      </div>
      <div class="kb-detail-row">
        <span class="kb-detail-label">应用包名</span>
        <span>{{ (kbDetailRow.metadata && kbDetailRow.metadata.app_package) || '-' }}</span>
      </div>
      <div class="kb-detail-row">
        <span class="kb-detail-label">时间</span>
        <span>{{ ((kbDetailRow.metadata && kbDetailRow.metadata.timestamp) || '').replace('T', ' ') }}</span>
      </div>
      <div class="kb-detail-row">
        <span class="kb-detail-label">相关度</span>
        <span>{{ kbDetailRow.score !== undefined ? (kbDetailRow.score.toFixed ? kbDetailRow.score.toFixed(4) : kbDetailRow.score) : '-' }}</span>
      </div>
      <div class="kb-detail-content">
        <div class="kb-detail-label" style="margin-bottom:8px">内容</div>
        <div class="kb-detail-text">{{ kbDetailRow.content }}</div>
      </div>
      <div v-if="kbDetailRow.metadata" class="kb-detail-content">
        <div class="kb-detail-label" style="margin-bottom:8px">元数据</div>
        <pre class="kb-detail-meta">{{ JSON.stringify(kbDetailRow.metadata, null, 2) }}</pre>
      </div>
    </div>
    <template #footer>
      <el-button type="primary" plain @click="editKbFromDetail">编辑</el-button>
      <el-button type="danger" plain @click="deleteKbItem(kbDetailRow)">删除此条</el-button>
      <el-button @click="kbDetailVisible = false">关闭</el-button>
    </template>
  </el-dialog>

  <!-- ═══════════ APP 增改对话框 ═══════════ -->
  <el-dialog v-model="appDialogVisible"
             :title="appDialogMode === 'add' ? '添加应用' : '编辑应用'"
             width="520px" :close-on-click-modal="false">
    <el-form :model="appForm" label-width="90px" style="padding-right:12px">
      <el-form-item label="应用名称" required>
        <el-input v-model="appForm.name" placeholder="如: Settings、设置" />
      </el-form-item>
      <el-form-item label="包名" required>
        <el-input v-model="appForm.package"
                  placeholder="如: com.android.settings"
                  :disabled="appDialogMode === 'edit'" />
        <div v-if="appDialogMode === 'edit'" style="font-size:12px;color:var(--text-muted);margin-top:4px">包名不可修改</div>
      </el-form-item>
      <el-form-item label="触发关键词">
        <div class="kw-editor">
          <div class="kw-list">
            <el-tag v-for="(kw, idx) in appForm.keywords" :key="idx"
                    closable @close="removeKeyword(idx)"
                    style="margin:3px">{{ kw }}</el-tag>
          </div>
          <div class="kw-input-row">
            <el-input v-model="newKeyword" placeholder="输入关键词后按 Enter 添加"
                      size="small" @keyup.enter="addKeyword" style="flex:1" />
            <el-button size="small" @click="addKeyword">添加</el-button>
          </div>
        </div>
        <div style="font-size:12px;color:var(--text-muted);margin-top:4px">
          用户输入测试指令时，包含任意关键词即自动匹配该应用
        </div>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="appDialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="appSaving" @click="saveApp">保存</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { onMounted, ref } from "vue";
import ReportDetail from "./components/ReportDetail.vue";
import KnowledgePanel from "./components/KnowledgePanel.vue";
import WorkspacePanel from "./components/WorkspacePanel.vue";
import DeviceFloat from "./components/DeviceFloat.vue";
import { ElMessage, ElMessageBox } from "element-plus";

const activeMenu = ref("workspace");
const executing = ref(false);
const wsConnected = ref(false);
const deviceOnline = ref(false);
const deviceInfo = ref(null);
const snapshotImage = ref("");
const pageSummary = ref("");
const elementList = ref([]);
const workspaceRef = ref(null);
const deviceFloatRef = ref(null);

// 配置管理
const configData = ref(null);
const configSaving = ref(false);

// 计划审阅
const planReviewVisible = ref(false);
const planReviewGoal = ref("");
const planReviewPages = ref([]);
const planReviewVerifications = ref([]);
const planReviewHints = ref([]);
const planReviewSubmitting = ref(false);
const newPageName = ref("");

function addReviewPage() {
  const name = newPageName.value.trim();
  if (name && !planReviewPages.value.includes(name)) {
    planReviewPages.value.push(name);
  }
  newPageName.value = "";
}

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

// 报告
const reportTasks = ref([]);
const reportDetailVisible = ref(false);
const selectedReport = ref(null);
const expandedSteps = ref(new Set());

// APP 管理
const appList = ref([]);
const appDialogVisible = ref(false);
const appDialogMode = ref('add');
const appSaving = ref(false);
const newKeyword = ref('');
const appForm = ref({ name: '', package: '', keywords: [] });

// 知识库
const kbList = ref([]);
const kbCount = ref(0);
const kbSearchQuery = ref('');
const kbFilterType = ref('');
const kbFilterPackage = ref('');
const kbDialogVisible = ref(false);
const kbDialogMode = ref('add');  // 'add' | 'edit'
const kbDetailVisible = ref(false);
const kbDetailRow = ref(null);
const kbSaving = ref(false);
const kbForm = ref({ app_package: '', knowledge_type: 'experience', content: '' });
const kbTypes = [
  { value: 'experience', label: '操作经验' },
  { value: 'curated_rule', label: '人工知识' },
];
const kbTypeColorMap = {
  experience: 'warning',
  curated_rule: 'success',
};
// 旧类型兼容映射（前端显示用）
const _legacyTypeMap = {
  verified_plan: 'experience',
  page_structure: 'experience',
  navigation_path: 'experience',
  test_experience: 'experience',
  app_precondition: 'curated_rule',
  global_knowledge: 'curated_rule',
};
function kbTypeLabel(type) {
  const resolved = _legacyTypeMap[type] || type;
  const t = kbTypes.find(x => x.value === resolved);
  return t ? t.label : (type || '未知');
}
function kbTypeColor(type) {
  const resolved = _legacyTypeMap[type] || type;
  return kbTypeColorMap[resolved] || 'info';
}

function toggleStepDetail(index) {
  const s = new Set(expandedSteps.value);
  if (s.has(index)) s.delete(index); else s.add(index);
  expandedSteps.value = s;
}

// ── 双维度结果映射 ──
const execStatusMap = {
  completed:     { label: '已完成', type: 'success' },
  exhausted:     { label: '步骤耗尽', type: 'warning' },
  error:         { label: '异常中断', type: 'danger' },
  cancelled:     { label: '已取消', type: 'info' },
  device_offline:{ label: '设备离线', type: 'info' },
};
const verdictMap = {
  passed:       { label: '通过', type: 'success' },
  failed:       { label: '未通过', type: 'danger' },
  inconclusive: { label: '待确认', type: 'warning' },
};
function execStatusLabel(s) { return (execStatusMap[s] || execStatusMap.error).label; }
function execStatusType(s)  { return (execStatusMap[s] || execStatusMap.error).type; }
function verdictLabel(s)    { return (verdictMap[s] || verdictMap.inconclusive).label; }
function verdictType(s)     { return (verdictMap[s] || verdictMap.inconclusive).type; }
function hasExtraConclusion(report) {
  if (!report.conclusion) return false;
  // 只有最后一个步骤有 observation，且结论与它不同时，才需要显示结论段
  const steps = report.steps || [];
  const lastObs = steps.length > 0 ? (steps[steps.length - 1].observation || '').trim() : '';
  const conclusion = (report.conclusion || '').trim();
  // 结论含 "已完成步骤:" 说明是失败摘要，必须显示
  if (conclusion.includes('已完成步骤:')) return true;
  // 结论与最后一步的 observation 相同则隐藏
  return conclusion !== lastObs;
}
function fmtDuration(ms) {
  if (!ms || ms <= 0) return '';
  if (ms < 1000) return ms + 'ms';
  if (ms < 60000) return (ms / 1000).toFixed(1) + 's';
  const m = Math.floor(ms / 60000);
  const s = Math.round((ms % 60000) / 1000);
  return s > 0 ? `${m}m${s}s` : `${m}m`;
}

let ws = null;
let snapshotInFlight = false;



// ═══════════ WebSocket 事件处理 ═══════════

function handleEvent(data) {
  const type = data.type;
  const content = data.content || {};
  const wp = workspaceRef.value;

  switch (type) {
    case "status": wp?.addEntry({ type: "log", text: String(content) }); refreshSnapshot(); break;
    case "plan_review": { const pd = content.plan || content; planReviewGoal.value = pd.goal || content.goal || ""; planReviewPages.value = pd.target_pages || content.pages || []; planReviewVerifications.value = pd.verification || content.verification || []; planReviewHints.value = pd.hints || []; planReviewVisible.value = true; wp?.addEntry({ type: "planner", icon: "🎯", text: planReviewGoal.value }); break; }

    case "plan_ready": wp?.addEntry({ type: "planner", icon: "🎯", text: content.goal || content.steps || "?" }); break;
    case "stream_token": wp?.onToken(); break;
    case "tool_start":
      wp?.addTool(
        content.name || "",
        (content.input || {}).label || "",
        content.intent_text || content.intent || ""
      );
      break;
    case "tool_end": wp?.finishTool(content.name || "", 0); break;
    case "step_start": refreshSnapshot(); break;
    case "step_end": refreshSnapshot(); break;
    case "snapshot": if (content.image) snapshotImage.value = content.image; break;
    case "device_status_change":
      deviceOnline.value = !!content.connected;
      break;
    case "knowledge_hint":
      wp?.addEntry({ type: "log", icon: "🧠", text: content.message || "建议先查询知识再执行动作" });
      break;
    case "anomaly": wp?.addEntry({ type: "error", icon: "⚠", text: content.message || content.description || "" }); break;

    case "need_human_approval": currentThreadId.value = content.thread_id || currentThreadId.value; humanQuestion.value = content.question || "是否继续执行?"; humanStep.value = content.step || 0; humanAction.value = content.action || ""; humanDialogVisible.value = true; executing.value = false; wp?.addEntry({ type: "log", icon: "⏸", text: "需要人工确认: " + humanQuestion.value }); break;
    case "result":
      if (content.status === "need_human" || content.interrupt) { const intr = content.interrupt || content; if (intr.type === "plan_review") { const planData = intr.plan || {}; planReviewGoal.value = planData.goal || intr.goal || ""; planReviewPages.value = planData.target_pages || intr.pages || []; planReviewVerifications.value = planData.verification || intr.verification || []; planReviewHints.value = planData.hints || []; planReviewVisible.value = true; currentThreadId.value = content.thread_id || ""; wp?.addEntry({ type: "log", icon: "⏸", text: "需要确认测试目标" }); } else { humanQuestion.value = intr.question || "是否继续?"; humanStep.value = intr.step || 0; humanAction.value = intr.action || ""; humanDialogVisible.value = true; wp?.addEntry({ type: "log", icon: "⏸", text: "需要人工确认" }); } executing.value = false; /* cleaned */; break; } { const pendingIds = content.pending_identities || []; if (content.status === "success" && pendingIds.length > 0) { const level2 = pendingIds.filter(p => p.level === 2); if (level2.length > 0) { identityPending.value = level2; identityDialogVisible.value = true; currentThreadId.value = content.thread_id || ""; wp?.addEntry({ type: "log", icon: "🔍", text: "发现 " + level2.length + " 个待确认的元素映射" }); } } } executing.value = false; /* cleaned */;
      // 工具调用已通过 tool_start/tool_end 事件实时推送，无需 fallback
      wp?.addResult(content.execution_status || "error", content.test_verdict || "inconclusive", content.conclusion || content.message || "", content.verification_results || []); refreshSnapshot(); loadReports(); break;
    case "error": wp?.addEntry({ type: "error", icon: "❌", text: String(content) }); executing.value = false; break;
    default: wp?.addEntry({ type: "log", text: "[" + type + "] " + JSON.stringify(content).substring(0, 200) });
  }
}

// ═══════════ WebSocket 连接 ═══════════

function connectWS() {
  ws = new WebSocket(`ws://${location.host}/ws/chat`);
  ws.onopen = () => {
    wsConnected.value = true;
  };
  ws.onclose = () => {
    wsConnected.value = false;
    setTimeout(connectWS, 3000);
  };
  ws.onmessage = (e) => handleEvent(JSON.parse(e.data));
}

// ═══════════ 一键执行 ═══════════

async function startRun(text) {
  if (!text || !text.trim()) return;
  text = text.trim();
  if (!deviceOnline.value) {
    ElMessage.warning("Android 设备未连接，请先连接设备");
    return;
  }
  // 检查 LLM 是否已配置
  const cfg = configData.value;
  if (!cfg || !cfg.api_key || cfg.api_key === '' || !cfg.model) {
    ElMessageBox.confirm(
      "首次使用请先配置 LLM 模型和 API Key，否则无法执行测试。",
      "未配置 LLM",
      { confirmButtonText: "去配置", cancelButtonText: "稍后再说", type: "warning" }
    ).then(() => { activeMenu.value = 'settings'; }).catch(() => {});
    return;
  }
  if (workspaceRef.value) workspaceRef.value.addEntry({ type: "user", icon: "🧑", text });
  executing.value = true;

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
  if (workspaceRef.value) workspaceRef.value.addEntry({ type: "log", text: "人工决定: " + decision });

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
    if (workspaceRef.value) workspaceRef.value.addEntry({ type: "log", text: "已确认 " + confirmed.length + " 个元素映射" });
  } catch (e) { /* ignore */ }
  identityDialogVisible.value = false;
}

// ═══════════ 计划审阅 ═══════════

async function confirmPlan(action) {
  if (planReviewSubmitting.value) return;  // 防抖：已在提交中
  planReviewSubmitting.value = true;
  planReviewVisible.value = false;
  executing.value = true;

  const resumePayload = action === "cancel" ? "cancel" : {
    action: "confirm",
    goal: planReviewGoal.value,
    target_pages: planReviewPages.value.filter(p => p.trim()),
    verification: planReviewVerifications.value.filter(v => v.trim()),
    hints: planReviewHints.value.filter(h => h.trim()),
  };

  if (workspaceRef.value) workspaceRef.value.addEntry({ type: "log", text: action === "cancel" ? "目标已取消" : `目标已确认: ${planReviewGoal.value}` });

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

// ═══════════ 设备操作 ═══════════

async function refreshSnapshot(force = false) {
  if (!(deviceFloatRef.value?.isVisible) && !force) return;  // 投屏窗口未打开时跳过
  if (snapshotInFlight && !force) return;
  snapshotInFlight = true;
  try {
    const res = await fetch(`/api/device/snapshot?t=${Date.now()}`, { cache: "no-store" });
    if (res.status === 503) {
      deviceOnline.value = false;
            return;
    }
    const data = await res.json();
    if (data.status === "error") {
      deviceOnline.value = false;
            return;
    }
    deviceOnline.value = true;
        snapshotImage.value = (data.screen || {}).image_base64 || "";
    pageSummary.value = (data.understanding || {}).summary || "";
    elementList.value = (data.understanding || {}).elements || [];
  } catch (e) {
    deviceOnline.value = false;
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
    setTimeout(() => refreshSnapshot(true), 350);
  } catch (e) {
    ElMessage.error(`按键失败: ${e}`);
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
    // 自动展开失败步骤
    const failIndices = ((data.report || {}).steps || []).filter(s => s.status === 'fail').map(s => s.index);
    expandedSteps.value = new Set(failIndices);
  } catch (e) {
    ElMessage.error(`读取失败: ${e}`);
  }
}

async function deleteReport(row) {
  if (!row?.id) return;
  try {
    await ElMessageBox.confirm(
      `确定删除测试报告 "${row.id}"？\n将同时清理关联截图、运行日志和数据库记录。`,
      "删除确认",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" }
    );
  } catch {
    return;
  }
  try {
    const res = await fetch(`/api/reports/${encodeURIComponent(row.id)}`, { method: "DELETE" });
    const data = await res.json();
    if (!res.ok || data.status !== "success") {
      ElMessage.error(data.message || "删除失败");
      return;
    }
    if (selectedReport.value && selectedReport.value.id === row.id) {
      reportDetailVisible.value = false;
      selectedReport.value = null;
    }
    const d = data.deleted || {};
    ElMessage.success(`删除成功（图片${d.images || 0}，日志${d.logs || 0}）`);
    await loadReports();
  } catch (e) {
    ElMessage.error(`删除失败: ${e}`);
  }
}

// ═══════════ APP 管理 ═══════════

async function loadApps() {
  try {
    const res = await fetch('/api/apps', { cache: 'no-store' });
    const data = await res.json();
    if (data.status === 'success') appList.value = data.apps || [];
  } catch (e) { /* ignore */ }
}

function openAppDialog(row) {
  if (row) {
    appDialogMode.value = 'edit';
    appForm.value = {
      name: row.name || '',
      package: row.package || '',
      keywords: [...(row.keywords || [])],
    };
  } else {
    appDialogMode.value = 'add';
    appForm.value = { name: '', package: '', keywords: [] };
  }
  newKeyword.value = '';
  appDialogVisible.value = true;
}

function addKeyword() {
  const kw = newKeyword.value.trim();
  if (!kw) return;
  if (!appForm.value.keywords.includes(kw)) {
    appForm.value.keywords.push(kw);
  }
  newKeyword.value = '';
}

function removeKeyword(idx) {
  appForm.value.keywords.splice(idx, 1);
}

async function saveApp() {
  if (!appForm.value.name.trim()) { ElMessage.warning('请输入应用名称'); return; }
  if (!appForm.value.package.trim()) { ElMessage.warning('请输入包名'); return; }
  appSaving.value = true;
  try {
    const isEdit = appDialogMode.value === 'edit';
    const url = isEdit ? `/api/apps/${encodeURIComponent(appForm.value.package)}` : '/api/apps';
    const res = await fetch(url, {
      method: isEdit ? 'PUT' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(appForm.value),
    });
    const data = await res.json();
    if (res.ok && data.status === 'success') {
      ElMessage.success(data.message || '保存成功');
      appDialogVisible.value = false;
      loadApps();
    } else {
      ElMessage.error(data.detail || data.message || '保存失败');
    }
  } catch (e) {
    ElMessage.error(`保存失败: ${e}`);
  } finally {
    appSaving.value = false;
  }
}

async function deleteApp(row) {
  try {
    await ElMessageBox.confirm(
      `确定删除应用 "${row.name}" (${row.package})？`,
      '删除确认', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    );
  } catch { return; } // 用户取消
  try {
    const res = await fetch(`/api/apps/${encodeURIComponent(row.package)}`, { method: 'DELETE' });
    const data = await res.json();
    if (res.ok && data.status === 'success') {
      ElMessage.success(data.message || '删除成功');
      loadApps();
    } else {
      ElMessage.error(data.detail || '删除失败');
    }
  } catch (e) {
    ElMessage.error(`删除失败: ${e}`);
  }
}

// ═══════════ 知识库 ═══════════

async function loadKbCount() {
  try {
    const res = await fetch('/api/knowledge/count', { cache: 'no-store' });
    const data = await res.json();
    if (data.status === 'success') kbCount.value = data.count || 0;
  } catch (e) { /* ignore */ }
}

async function loadKbList() {
  await loadKbCount();
  const params = new URLSearchParams();
  if (kbFilterPackage.value) params.set('app_package', kbFilterPackage.value);
  if (kbFilterType.value) params.set('knowledge_type', kbFilterType.value);
  params.set('top_k', '100');
  try {
    const res = await fetch(`/api/knowledge/list?${params}`, { cache: 'no-store' });
    const data = await res.json();
    if (data.status === 'success') kbList.value = data.items || [];
  } catch (e) { /* ignore */ }
}

async function searchKb() {
  const q = kbSearchQuery.value.trim();
  if (!q) { loadKbList(); return; }
  try {
    const res = await fetch('/api/knowledge/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        query: q,
        app_package: kbFilterPackage.value,
        knowledge_type: kbFilterType.value,
        top_k: 50,
      }),
    });
    const data = await res.json();
    if (data.status === 'success') kbList.value = data.results || [];
  } catch (e) { ElMessage.error(`搜索失败: ${e}`); }
}

function resetKbFilter() {
  kbSearchQuery.value = '';
  kbFilterType.value = '';
  kbFilterPackage.value = '';
  loadKbList();
}

function openKbDialog() {
  kbDialogMode.value = 'add';
  kbForm.value = { app_package: '', knowledge_type: 'experience', content: '' };
  kbDialogVisible.value = true;
}

function editKbFromDetail() {
  if (!kbDetailRow.value) return;
  const row = kbDetailRow.value;
  kbDialogMode.value = 'edit';
  kbForm.value = {
    app_package: (row.metadata && row.metadata.app_package) || '',
    knowledge_type: (row.metadata && row.metadata.knowledge_type) || 'experience',
    content: row.content || '',
  };
  kbDetailVisible.value = false;
  kbDialogVisible.value = true;
}

async function saveKb() {
  if (!kbForm.value.app_package.trim() && kbForm.value.knowledge_type !== 'curated_rule') {
    ElMessage.warning('请输入应用包名'); return;
  }
  if (!kbForm.value.content.trim()) { ElMessage.warning('请输入知识内容'); return; }
  kbSaving.value = true;
  try {
    if (kbDialogMode.value === 'edit' && kbDetailRow.value) {
      // 编辑模式：PUT 请求（删旧+加新）
      const oldRow = kbDetailRow.value;
      const res = await fetch('/api/knowledge', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          old_entry_id: oldRow.id || '',
          old_content: oldRow.content || '',
          old_app_package: (oldRow.metadata && oldRow.metadata.app_package) || '',
          old_knowledge_type: (oldRow.metadata && oldRow.metadata.knowledge_type) || '',
          new_entry: kbForm.value,
        }),
      });
      const data = await res.json();
      if (res.ok && data.status === 'success') {
        ElMessage.success('知识已更新');
        kbDialogVisible.value = false;
        kbDetailRow.value = null;
        loadKbList();
      } else {
        ElMessage.error(data.detail || data.message || '更新失败');
      }
    } else {
      // 新增模式：POST 请求
      const res = await fetch('/api/knowledge', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(kbForm.value),
      });
      const data = await res.json();
      if (res.ok && data.status === 'success') {
        ElMessage.success('知识已添加');
        kbDialogVisible.value = false;
        loadKbList();
      } else {
        ElMessage.error(data.detail || data.message || '保存失败');
      }
    }
  } catch (e) {
    ElMessage.error(`保存失败: ${e}`);
  } finally {
    kbSaving.value = false;
  }
}

function openKbDetail(row) {
  kbDetailRow.value = row;
  kbDetailVisible.value = true;
}

async function deleteKbItem(row) {
  if (!row) return;
  const entryId = row.id || '';
  const pkg = (row.metadata && row.metadata.app_package) || '';
  const type = (row.metadata && row.metadata.knowledge_type) || '';
  const content = row.content || '';
  if (!pkg && !type) { ElMessage.warning('无法定位该条知识'); return; }
  try {
    await ElMessageBox.confirm(
      `确定删除此条知识？\n包名: ${pkg || '-'}  类型: ${kbTypeLabel(type)}`,
      '删除确认', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    );
  } catch { return; }
  const params = new URLSearchParams();
  if (entryId) params.set('entry_id', entryId);
  if (pkg) params.set('app_package', pkg);
  if (type) params.set('knowledge_type', type);
  if (content) params.set('content', content);
  try {
    const res = await fetch(`/api/knowledge?${params}`, { method: 'DELETE' });
    const data = await res.json();
    if (res.ok && data.status === 'success') {
      ElMessage.success(`已删除 ${data.deleted || 0} 条`);
      kbDetailVisible.value = false;
      loadKbList();
    } else {
      ElMessage.error(data.detail || data.message || '删除失败');
    }
  } catch (e) {
    ElMessage.error(`删除失败: ${e}`);
  }
}

async function deleteKbByFilter() {
  if (!kbFilterPackage.value && !kbFilterType.value) {
    ElMessage.warning('请至少选择应用包名或知识类型');
    return;
  }
  const label = [kbFilterPackage.value, kbTypeLabel(kbFilterType.value)].filter(Boolean).join(' / ');
  try {
    await ElMessageBox.confirm(
      `确定批量删除 「${label}」 相关知识？此操作不可恢复。`,
      '批量删除确认', { type: 'error', confirmButtonText: '全部删除', cancelButtonText: '取消' }
    );
  } catch { return; }
  const params = new URLSearchParams();
  if (kbFilterPackage.value) params.set('app_package', kbFilterPackage.value);
  if (kbFilterType.value) params.set('knowledge_type', kbFilterType.value);
  try {
    const res = await fetch(`/api/knowledge?${params}`, { method: 'DELETE' });
    const data = await res.json();
    if (res.ok && data.status === 'success') {
      ElMessage.success(`已删除 ${data.deleted || 0} 条`);
      loadKbList();
    } else {
      ElMessage.error(data.detail || data.message || '删除失败');
    }
  } catch (e) {
    ElMessage.error(`删除失败: ${e}`);
  }
}

// ═══════════ 设备状态轮询 ═══════════

async function checkDeviceStatus() {
  try {
    const res = await fetch("/api/device/status", { cache: "no-store" });
    const data = await res.json();
    const wasOffline = !deviceOnline.value;
    deviceOnline.value = !!data.connected;
    if (data.connected && wasOffline) {
      fetchDeviceInfo();
      if (deviceFloatRef.value?.isVisible) refreshSnapshot(true);
    } else if (!data.connected && !wasOffline) {
      deviceInfo.value = null;
    }
  } catch (e) {
    deviceOnline.value = false;
    deviceInfo.value = null;
  }
}

async function fetchDeviceInfo() {
  try {
    const res = await fetch("/api/device/info", { cache: "no-store" });
    const data = await res.json();
    if (data.connected) deviceInfo.value = data;
  } catch (e) { /* ignore */ }
}

async function reconnectDevice() {
  deviceInfo.value = null;
  deviceOnline.value = false;
  try {
    const res = await fetch("/api/device/reconnect", { method: "POST" });
    const data = await res.json();
    if (data.connected) {
      deviceOnline.value = true;
      fetchDeviceInfo();
            if (deviceFloatRef.value?.isVisible) refreshSnapshot(true);
    } else {
      ElMessage.warning(data.detail || "设备重连失败");
    }
  } catch (e) {
    ElMessage.error("重连请求失败");
  }
}

// ═══════════ 配置管理 ═══════════

async function fetchConfig() {
  try {
    const res = await fetch("/api/config", { cache: "no-store" });
    configData.value = await res.json();
  } catch (e) {
    ElMessage.error("加载配置失败");
  }
}

async function saveConfig() {
  if (!configData.value) return;
  configSaving.value = true;
  try {
    const res = await fetch("/api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(configData.value),
    });
    const data = await res.json();
    if (data.status === "success") {
      ElMessage.success("配置已保存");
      // 重新加载以获取脱敏后的 API Key
      await fetchConfig();
    } else {
      ElMessage.error("保存失败");
    }
  } catch (e) {
    ElMessage.error("保存配置失败");
  } finally {
    configSaving.value = false;
  }
}

onMounted(() => {
  connectWS();
  checkDeviceStatus();
  loadReports();
  loadApps();
  loadKbList();
  fetchConfig();
});

</script>

<style scoped src="./App.css"></style>

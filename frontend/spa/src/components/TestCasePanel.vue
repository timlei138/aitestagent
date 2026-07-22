<template>
  <div class="tcp-root">
    <!-- 工具条 -->
    <div class="tcp-toolbar">
      <el-input v-model="searchQuery" size="small" placeholder="搜索用例名称..." style="width:240px" clearable @clear="fetchCases" @keyup.enter="fetchCases" />
      <div class="tcp-toolbar-right">
        <el-dropdown @command="handleAddMode" :disabled="executing" trigger="click">
          <el-button size="small" type="primary" :disabled="executing">
            + 新增用例 <el-icon class="el-icon--right"><ArrowDown /></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="from_report">从报告复制</el-dropdown-item>
              <el-dropdown-item command="from_scratch">从零新建</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <el-button v-if="selectedIds.length > 0" size="small" type="danger" @click="batchDelete" :disabled="executing">
          批量删除（{{ selectedIds.length }}）
        </el-button>
      </div>
    </div>

    <!-- 表格 -->
    <el-table
      ref="tableRef"
      :data="cases"
      empty-text="暂无用例，请保存成功报告或手动新增"
      row-key="id"
      @selection-change="onSelectionChange"
    >
      <el-table-column type="selection" width="40" />
      <el-table-column prop="name" label="名称" min-width="150" show-overflow-tooltip />
      <el-table-column prop="user_request" label="来源请求" min-width="160" show-overflow-tooltip>
        <template #default="{ row }">
          <el-tooltip :content="row.user_request" placement="top" :disabled="!row.user_request">
            <span>{{ (row.user_request || '').substring(0, 40) }}{{ (row.user_request || '').length > 40 ? '…' : '' }}</span>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column prop="app_package" label="应用" width="150" show-overflow-tooltip />
      <el-table-column label="最近状态" width="110">
        <template #default="{ row }">
          <el-tag v-if="!row.last_run_status" size="small" type="info" effect="plain">未运行</el-tag>
          <el-tag v-else-if="statusPassed(row.last_run_status)" size="small" type="success">通过</el-tag>
          <el-tag v-else-if="row.last_run_status.includes('failed')" size="small" type="danger">未通过</el-tag>
          <el-tag v-else-if="row.last_run_status.startsWith('busy')" size="small" type="warning">运行中</el-tag>
          <el-tag v-else size="small" type="warning">待复核</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="created_at" label="创建时间" width="130">
        <template #default="{ row }">{{ (row.created_at || '').substring(0, 10) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="180" fixed="right">
        <template #default="{ row }">
          <div style="white-space:nowrap">
            <el-button size="small" text type="primary"
              :disabled="executing || !hasGoal(row)"
              :title="!hasGoal(row) ? '该用例无可用计划' : ''"
              @click="runCase(row.id)">运行</el-button>
            <el-button size="small" text @click="editCase(row)">编辑</el-button>
            <el-button size="small" text type="danger" :disabled="executing" @click="deleteCase(row.id)">删除</el-button>
          </div>
        </template>
      </el-table-column>
    </el-table>

    <!-- 新增/编辑对话框 -->
    <el-dialog
      v-model="dialogVisible"
      :title="dialogMode === 'edit' ? (isManagedEvidenceCase ? '查看回放证据' : '编辑用例计划') : '新增用例'"
      width="700px"
      destroy-on-close
    >
      <!-- 从报告复制模式：报告选择器 -->
      <div v-if="dialogMode === 'from_report'" style="margin-bottom:16px">
        <el-input v-model="reportSearch" size="small" placeholder="搜索报告..." style="margin-bottom:8px" clearable />
        <el-table
          :data="filteredReports"
          height="200"
          highlight-current-row
          @row-click="pickReport"
          style="cursor:pointer"
        >
          <el-table-column prop="user_request" label="请求" show-overflow-tooltip />
          <el-table-column prop="created_at" label="时间" width="140">
            <template #default="{ row }">{{ (row.created_at || '').substring(0, 16) }}</template>
          </el-table-column>
          <el-table-column label="结论" width="70">
            <template #default="{ row }">
              <el-tag :type="row.test_verdict === 'passed' ? 'success' : 'warning'" size="small">
                {{ row.test_verdict === 'passed' ? '通过' : '未通过' }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <el-form :model="form" label-position="top">
        <template v-if="isManagedEvidenceCase">
          <el-form-item label="用例名称">
            <el-input v-model="form.name" placeholder="如：WIFI 开关验证" />
          </el-form-item>
          <el-form-item label="应用包名">
            <el-input v-model="form.app_package" placeholder="如：com.android.settings" />
          </el-form-item>
          <el-form-item label="应用名称">
            <el-input v-model="form.app_name" placeholder="如：Settings" />
          </el-form-item>

          <el-alert
            title="回放证据由服务端派生并只读展示；本页不会提交 base_evidence、effective、goal_json 或 execution_plan。"
            type="info"
            :closable="false"
            show-icon
          />
          <section class="tcp-exec-plan">
            <div class="tcp-evidence-heading">
              <strong>有效回放证据</strong>
              <el-tag size="small" type="info">revision {{ effectiveRevision || '未记录' }}</el-tag>
            </div>

            <div class="tcp-evidence-section">
              <span class="tcp-evidence-label">入口</span>
              <span>{{ entrySummary(effectiveEvidence.entry) }}</span>
            </div>
            <div class="tcp-evidence-section">
              <span class="tcp-evidence-label">前置入口</span>
              <span>{{ preEntrySummary(effectiveEvidence.pre_entry) }}</span>
            </div>

            <div class="tcp-evidence-heading tcp-actions-heading">
              <strong>关键动作（{{ effectiveKeyActions.length }}）</strong>
            </div>
            <el-table :data="effectiveKeyActions" size="small" max-height="220" empty-text="未记录关键动作">
              <el-table-column label="动作" min-width="120">
                <template #default="{ row, $index }">{{ actionSummary(row, $index) }}</template>
              </el-table-column>
              <el-table-column label="定位/参数" min-width="190" show-overflow-tooltip>
                <template #default="{ row }">{{ actionTargetSummary(row) }}</template>
              </el-table-column>
              <el-table-column label="结果" width="100" show-overflow-tooltip>
                <template #default="{ row }">{{ actionResultSummary(row) }}</template>
              </el-table-column>
            </el-table>

            <div class="tcp-evidence-heading tcp-actions-heading">
              <strong>能力状态</strong>
            </div>
            <div class="tcp-capabilities">
              <el-tag
                v-for="capability in replayCapabilities"
                :key="capability.key"
                size="small"
                :type="capability.ok ? 'success' : capability.warning ? 'warning' : 'info'"
                effect="plain"
              >{{ capability.label }} {{ capability.value }}</el-tag>
            </div>
          </section>
        </template>

        <!-- v3/new-case editable plan -->
        <template v-else>
          <el-form-item label="用例名称">
            <el-input v-model="form.name" placeholder="如：WIFI 开关验证" />
          </el-form-item>
          <el-form-item label="应用包名">
            <el-input v-model="form.app_package" placeholder="如：com.android.settings" />
          </el-form-item>
          <el-form-item label="应用名称">
            <el-input v-model="form.app_name" placeholder="如：Settings" />
          </el-form-item>
          <el-form-item label="测试目标">
            <el-input v-model="form.goal" type="textarea" :rows="2" placeholder="一句话描述测试目标" />
          </el-form-item>
          <el-form-item label="目标页面">
            <div v-for="(p, i) in form.target_pages" :key="i" class="tcp-list-row">
              <el-input v-model="form.target_pages[i]" size="small" />
              <el-button size="small" type="danger" text @click="form.target_pages.splice(i,1)">×</el-button>
            </div>
            <el-button size="small" @click="form.target_pages.push('')">+ 添加页面</el-button>
          </el-form-item>
          <el-form-item label="验证条件">
            <div v-for="(v, i) in form.verification" :key="i" class="tcp-list-row">
              <el-input v-model="form.verification[i]" size="small" />
              <el-button size="small" type="danger" text @click="form.verification.splice(i,1)">×</el-button>
            </div>
            <el-button size="small" @click="form.verification.push('')">+ 添加验证</el-button>
          </el-form-item>
          <el-form-item label="导航提示">
            <div v-for="(h, i) in form.hints" :key="i" class="tcp-list-row">
              <el-input v-model="form.hints[i]" size="small" />
              <el-button size="small" type="danger" text @click="form.hints.splice(i,1)">×</el-button>
            </div>
            <el-button size="small" @click="form.hints.push('')">+ 添加提示</el-button>
          </el-form-item>
        </template>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="saveCase">{{ dialogMode === 'edit' ? (isManagedEvidenceCase ? '保存元数据' : '保存') : '创建' }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowDown } from '@element-plus/icons-vue'

const props = defineProps({
  reports: { type: Array, default: () => [] },
  executing: { type: Boolean, default: false },
})
const emit = defineEmits(['run-case', 'refresh'])

function statusPassed(s) {
  if (!s) return false
  const parts = s.split('/')
  return parts[0] === 'completed' && parts[1] === 'passed'
}

function parseGoal(value) {
  if (!value) return {}
  if (typeof value === 'object') return value
  try { return JSON.parse(value) || {} } catch { return {} }
}

function managedEvidenceFor(row) {
  const evidence = row?.plan_capabilities?.replay_evidence
  return evidence && typeof evidence === 'object' ? evidence : null
}

function isCaseRunnable(row) {
  const capabilities = row?.plan_capabilities
  if (capabilities && typeof capabilities === 'object' && 'can_run' in capabilities) {
    return Boolean(capabilities.can_run)
  }
  // Compatibility fallback for cases returned by an older backend. It deliberately
  // avoids interpreting execution-plan storage formats in the UI.
  const goal = parseGoal(row && row.goal_json)
  return Boolean(goal.goal || (goal.target_pages && goal.target_pages.length) || (goal.verification && goal.verification.length))
}

function hasGoal(row) {
  return isCaseRunnable(row)
}

const cases = ref([])
const searchQuery = ref('')
const selectedIds = ref([])
const tableRef = ref(null)

// 对话框
const dialogVisible = ref(false)
const dialogMode = ref('from_scratch') // 'from_report' | 'from_scratch' | 'edit'
const editingId = ref('')
const editingCase = ref(null)
const reportSearch = ref('')
const pickedReport = ref(null)

const form = ref({
  name: '',
  app_package: '',
  app_name: '',
  goal: '',
  target_pages: [],
  verification: [],
  hints: [],
})

const isManagedEvidenceCase = computed(() => (
  editingCase.value?.plan_capabilities?.evidence_management === 'server_managed'
))
const effectiveEvidence = computed(() => managedEvidenceFor(editingCase.value) || {})
const effectiveKeyActions = computed(() => Array.isArray(effectiveEvidence.value.key_actions) ? effectiveEvidence.value.key_actions : [])
const effectiveRevision = computed(() => effectiveEvidence.value.effective_revision || 0)

const filteredReports = computed(() => {
  const base = props.reports.filter(r => r.run_type !== 'rerun')
  if (!reportSearch.value) return base
  const q = reportSearch.value.toLowerCase()
  return base.filter(r => (r.user_request || '').toLowerCase().includes(q))
})

const replayCapabilities = computed(() => (
  capabilityItems(editingCase.value?.plan_capabilities || {})
))

function capabilityItems(capabilities) {
  return [
    { key: 'entry', label: '入口', value: capabilities.has_verified_entry ? '已验证' : '未验证', ok: Boolean(capabilities.has_verified_entry) },
    { key: 'locator', label: '定位', value: capabilities.has_stable_locator ? '稳定' : (capabilities.index_only_locator ? '仅序号' : '未记录'), ok: Boolean(capabilities.has_stable_locator), warning: Boolean(capabilities.index_only_locator) },
    { key: 'objective', label: '客观证据', value: capabilities.has_objective_evidence ? '已记录' : '未记录', ok: Boolean(capabilities.has_objective_evidence) },
    { key: 'stale', label: '证据', value: capabilities.evidence_stale ? '待复核' : '最新', ok: !capabilities.evidence_stale, warning: Boolean(capabilities.evidence_stale) },
    { key: 'revision', label: '修订', value: `r${capabilities.effective_revision || 0}`, ok: true },
  ]
}

function entrySummary(entry) {
  if (!entry) return '未记录（可能已处于目标页）'
  const launch = entry.launch_app_args || {}
  const postcondition = entry.postcondition || {}
  const target = [launch.package, launch.activity].filter(Boolean).join(' / ')
  const observed = postcondition.observed_activity || postcondition.expected_activity || ''
  return [target || '已记录入口', observed && `到达 ${observed}`, postcondition.arrival_confirmed ? '已确认' : '待确认'].filter(Boolean).join('；')
}

function preEntrySummary(preEntry) {
  if (!preEntry) return '未记录'
  const activity = preEntry.expected_activity || preEntry.activity || ''
  const anchors = Array.isArray(preEntry.required_anchors) ? preEntry.required_anchors.map(anchor => anchor.label).filter(Boolean).join('、') : ''
  return [activity, anchors].filter(Boolean).join('；') || '已记录'
}

function actionSummary(action, index) {
  return action.step || action.tool || `动作 ${index + 1}`
}

function actionTargetSummary(action) {
  const locator = action.preferred_locator || action.resolved_target || {}
  const label = locator.label || ''
  const locatorText = [label, locator.rid, locator.path_contains || locator.path].filter(Boolean).join(' · ')
  const args = action.args || {}
  return locatorText || args.text || action.verify_key || '—'
}

function actionResultSummary(action) {
  return action.last_result || action.last_status_code || '—'
}

// ── 数据 ──
async function fetchCases() {
  try {
    const q = searchQuery.value ? '?q=' + encodeURIComponent(searchQuery.value) : ''
    const r = await fetch('/api/test_cases' + q)
    const d = await r.json()
    if (d.status === 'ok') cases.value = d.data || []
  } catch (e) { /* ignore */ }
}

function resetForm() {
  form.value = { name: '', app_package: '', app_name: '', goal: '', target_pages: [], verification: [], hints: [] }
  pickedReport.value = null
  editingCase.value = null
  editingId.value = ''
}

function handleAddMode(cmd) {
  resetForm()
  dialogMode.value = cmd
  dialogVisible.value = true
}

function pickReport(row) {
  pickedReport.value = row
  const goal = parseGoal(row.goal_json)
  form.value.name = (row.user_request || '').substring(0, 40)
  form.value.app_package = goal.app_package || row.app_package || ''
  form.value.app_name = goal.app_name || row.app_name || ''
  form.value.goal = goal.goal || ''
  form.value.target_pages = [...(goal.target_pages || [])]
  form.value.verification = [...(goal.verification || [])]
  form.value.hints = [...(goal.hints || [])]
}

function editCase(row) {
  resetForm()
  dialogMode.value = 'edit'
  editingId.value = row.id
  editingCase.value = row
  const goal = parseGoal(row.goal_json)
  form.value.name = row.name || ''
  form.value.app_package = row.app_package || goal.app_package || ''
  form.value.app_name = row.app_name || goal.app_name || ''
  form.value.goal = goal.goal || ''
  form.value.target_pages = [...(goal.target_pages || [])]
  form.value.verification = [...(goal.verification || [])]
  form.value.hints = [...(goal.hints || [])]
  dialogVisible.value = true
}

async function saveManagedEvidenceMetadata() {
  // Server-managed Replay Evidence is read-only in this view. Do not manufacture
  // evidence patches or submit plan data while changing case metadata.
  const body = {
    name: form.value.name || '未命名',
    user_request: form.value.name || '',
    app_package: form.value.app_package,
    app_name: form.value.app_name,
  }
  return fetch('/api/test_cases/' + editingId.value, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

async function saveCase() {
  let r
  try {
    if (dialogMode.value === 'edit' && isManagedEvidenceCase.value) {
      r = await saveManagedEvidenceMetadata()
    } else {
      const goalJson = {
        goal: form.value.goal,
        app_package: form.value.app_package,
        app_name: form.value.app_name,
        target_pages: form.value.target_pages.filter(Boolean),
        verification: form.value.verification.filter(Boolean),
        hints: form.value.hints.filter(Boolean),
      }
      const body = {
        name: form.value.name || '未命名',
        user_request: form.value.name || '',
        app_package: form.value.app_package,
        app_name: form.value.app_name,
        goal_json: goalJson,
      }
      if (dialogMode.value === 'from_report' && pickedReport.value) {
        body.run_id = pickedReport.value.id
      }
      r = await fetch(dialogMode.value === 'edit' ? '/api/test_cases/' + editingId.value : '/api/test_cases', {
        method: dialogMode.value === 'edit' ? 'PUT' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
    }
    const d = await r.json()
    if (d.status === 'ok') {
      ElMessage.success(dialogMode.value === 'edit' ? '已更新' : '已创建')
      dialogVisible.value = false
      fetchCases()
      emit('refresh')
    } else {
      ElMessage.error(d.message || '操作失败')
    }
  } catch (e) { ElMessage.error('操作失败: ' + e) }
}

async function deleteCase(id) {
  try {
    const r = await fetch('/api/test_cases/' + id, { method: 'DELETE' })
    const d = await r.json()
    if (d.status === 'ok') { fetchCases(); emit('refresh') }
  } catch (e) { ElMessage.error('删除失败') }
}

async function batchDelete() {
  try {
    await ElMessageBox.confirm(`确定删除 ${selectedIds.value.length} 个用例？`, '批量删除', {
      type: 'warning', confirmButtonText: '确定', cancelButtonText: '取消',
    })
    await fetch('/api/test_cases/batch_delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: selectedIds.value }),
    })
    selectedIds.value = []
    fetchCases()
    emit('refresh')
  } catch (e) { /* cancelled or error */ }
}

function runCase(id) {
  emit('run-case', id)
}

function onSelectionChange(val) {
  selectedIds.value = val.map(r => r.id)
}

onMounted(fetchCases)
defineExpose({ fetchCases })
</script>

<style scoped>
.tcp-root { display: flex; flex-direction: column; gap: 10px; }
.tcp-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.tcp-toolbar-right { display: flex; align-items: center; gap: 8px; }
.tcp-list-row { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }
.tcp-exec-plan { margin-top: 14px; padding: 12px; border: 1px solid var(--el-border-color-light); border-radius: 4px; background: var(--el-fill-color-lighter); }
.tcp-evidence-heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 8px; }
.tcp-actions-heading { margin-top: 14px; }
.tcp-evidence-section { display: grid; grid-template-columns: 74px minmax(0, 1fr); gap: 8px; margin: 6px 0; line-height: 1.45; word-break: break-word; }
.tcp-evidence-label { color: var(--el-text-color-secondary); }
.tcp-capabilities { display: flex; flex-wrap: wrap; gap: 6px; }
</style>

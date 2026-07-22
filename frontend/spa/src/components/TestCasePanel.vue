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
      :title="dialogMode === 'edit' ? '编辑用例计划' : '新增用例'"
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

      <!-- 计划表单 -->
      <el-form :model="form" label-position="top">
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
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="saveCase">{{ dialogMode === 'edit' ? '保存' : '创建' }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowDown } from '@element-plus/icons-vue'

const props = defineProps({
  reports: { type: Array, default: () => [] },
  executing: { type: Boolean, default: false },
})
const emit = defineEmits(['run-case', 'refresh'])

// ── 辅助 ──
function statusPassed(s) {
  if (!s) return false
  const parts = s.split('/')
  return parts[0] === 'completed' && parts[1] === 'passed'
}
function hasGoal(row) {
  if (!row || !row.goal_json) return false
  if (row.goal_json === '{}' || row.goal_json === '') return false
  try {
    const g = typeof row.goal_json === 'string' ? JSON.parse(row.goal_json) : row.goal_json
    if (!g || (typeof g === 'object' && Object.keys(g).length === 0)) return false
    return !!(g.goal || (g.target_pages && g.target_pages.length) || (g.verification && g.verification.length))
  } catch { return false }
}

const cases = ref([])
const searchQuery = ref('')
const selectedIds = ref([])
const tableRef = ref(null)

// 对话框
const dialogVisible = ref(false)
const dialogMode = ref('from_scratch') // 'from_report' | 'from_scratch' | 'edit'
const editingId = ref('')
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

const filteredReports = computed(() => {
  const base = props.reports.filter(r => r.run_type !== 'rerun')
  if (!reportSearch.value) return base
  const q = reportSearch.value.toLowerCase()
  return base.filter(r => (r.user_request || '').toLowerCase().includes(q))
})

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
}

function handleAddMode(cmd) {
  resetForm()
  dialogMode.value = cmd
  dialogVisible.value = true
}

function pickReport(row) {
  pickedReport.value = row
  try {
    const g = typeof row.goal_json === 'string' ? JSON.parse(row.goal_json) : (row.goal_json || {})
    form.value.name = (row.user_request || '').substring(0, 40)
    form.value.app_package = g.app_package || row.app_package || ''
    form.value.app_name = g.app_name || row.app_name || ''
    form.value.goal = g.goal || ''
    form.value.target_pages = [...(g.target_pages || [])]
    form.value.verification = [...(g.verification || [])]
    form.value.hints = [...(g.hints || [])]
  } catch (e) { ElMessage.error('计划数据解析失败'); }
}

function editCase(row) {
  resetForm()
  dialogMode.value = 'edit'
  editingId.value = row.id
  try {
    const g = typeof row.goal_json === 'string' ? JSON.parse(row.goal_json) : (row.goal_json || {})
    form.value.name = row.name || ''
    form.value.app_package = g.app_package || row.app_package || ''
    form.value.app_name = g.app_name || row.app_name || ''
    form.value.goal = g.goal || ''
    form.value.target_pages = [...(g.target_pages || [])]
    form.value.verification = [...(g.verification || [])]
    form.value.hints = [...(g.hints || [])]
  } catch (e) { ElMessage.error('计划数据解析失败'); }
  dialogVisible.value = true
}

async function saveCase() {
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
  // 从报告复制：追加 run_id
  if (dialogMode.value === 'from_report' && pickedReport.value) {
    body.run_id = pickedReport.value.id
  }

  try {
    let r
    if (dialogMode.value === 'edit') {
      r = await fetch('/api/test_cases/' + editingId.value, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
    } else {
      r = await fetch('/api/test_cases', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
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
  } catch (e) { ElMessage.error('操作失败: ' + e); }
}

async function deleteCase(id) {
  try {
    const r = await fetch('/api/test_cases/' + id, { method: 'DELETE' })
    const d = await r.json()
    if (d.status === 'ok') { fetchCases(); emit('refresh'); }
  } catch (e) { ElMessage.error('删除失败'); }
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
  // 临时禁用按钮，等待 App.vue 的 runCase 处理
  // App.vue 会在 run 结束后 emit('refresh') 触发 fetchCases
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
</style>

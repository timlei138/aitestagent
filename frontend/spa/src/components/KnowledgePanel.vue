<template>
  <section class="panel">
    <div class="panel-header">
      <h3 class="panel-title">知识库管理</h3>
      <div style="display:flex;gap:8px;align-items:center">
        <el-tag size="small" type="info">共 {{ kbCount }} 条</el-tag>
        <el-button size="small" @click="$emit('refresh')">刷新</el-button>
        <el-button size="small" type="primary" @click="$emit('add')">+ 新增知识</el-button>
      </div>
    </div>
    <div class="kb-filter-bar">
      <el-input v-model="searchQuery" placeholder="搜索关键词（语义检索）" size="small"
                style="width:220px" clearable />
      <el-select v-model="filterType" placeholder="知识类型" size="small"
                 style="width:150px" clearable>
        <el-option v-for="t in types" :key="t.value" :label="t.label" :value="t.value" />
      </el-select>
      <el-input v-model="filterPackage" placeholder="应用包名" size="small"
                style="width:200px" clearable />
      <el-button size="small" type="primary" @click="$emit('search', { query: searchQuery, type: filterType, pkg: filterPackage })">搜索</el-button>
      <el-button size="small" @click="reset">重置</el-button>
      <el-button size="small" type="danger" plain
                 :disabled="!filterPackage && !filterType"
                 @click="$emit('deleteByFilter', { type: filterType, pkg: filterPackage })">批量删除</el-button>
    </div>
    <el-table :data="list" border stripe empty-text="暂无知识数据" style="margin-top:12px">
      <el-table-column type="index" width="50" />
      <el-table-column label="知识类型" width="120">
        <template #default="{ row }">
          <el-tag :type="typeColor(row.metadata && row.metadata.knowledge_type)" size="small">
            {{ typeLabel(row.metadata && row.metadata.knowledge_type) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="应用包名" min-width="160" show-overflow-tooltip>
        <template #default="{ row }">{{ (row.metadata && row.metadata.app_package) || '-' }}</template>
      </el-table-column>
      <el-table-column label="内容" min-width="280" show-overflow-tooltip>
        <template #default="{ row }">{{ row.content }}</template>
      </el-table-column>
      <el-table-column label="时间" width="120">
        <template #default="{ row }">
          {{ ((row.metadata && row.metadata.timestamp) || '').substring(0, 16).replace('T', ' ') }}
        </template>
      </el-table-column>
      <el-table-column label="操作" width="80" fixed="right">
        <template #default="{ row }">
          <el-button size="small" text type="primary" @click.stop="$emit('detail', row)">详情</el-button>
        </template>
      </el-table-column>
    </el-table>
  </section>
</template>

<script setup>
import { ref } from 'vue'

defineProps({
  list: { type: Array, default: () => [] },
  kbCount: { type: Number, default: 0 },
  types: { type: Array, default: () => [] },
})

defineEmits(['refresh', 'add', 'search', 'deleteByFilter', 'detail', 'resetFilter'])

const searchQuery = ref('')
const filterType = ref('')
const filterPackage = ref('')

function reset() {
  searchQuery.value = ''
  filterType.value = ''
  filterPackage.value = ''
}

const _legacyMap = { page_structure: 'experience', navigation_path: 'experience', test_experience: 'experience', app_precondition: 'curated_rule', global_knowledge: 'curated_rule' }
const _typeMap = [
  { value: 'experience', label: '操作经验' },
  { value: 'curated_rule', label: '人工知识' },
]
const _colorMap = { experience: 'warning', curated_rule: 'success' }
function typeLabel(t) { const r = _legacyMap[t] || t; const f = _typeMap.find(x => x.value === r); return f ? f.label : (t || '未知') }
function typeColor(t) { const r = _legacyMap[t] || t; return _colorMap[r] || 'info' }
</script>

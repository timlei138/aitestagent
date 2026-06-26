<template>
  <section class="wp-root">
    <!-- 时间线 -->
    <div class="wp-timeline" ref="timelineRef">
      <div v-for="entry in timeline" :key="entry.id" class="wp-entry" :class="'wp-' + entry.type">
        <!-- thinking -->
        <template v-if="entry.type === 'thinking'">
          <span class="wp-spinner"></span>
          <span class="wp-text">{{ entry.text }}</span>
          <span class="wp-time">{{ entry.time }}</span>
        </template>
        <!-- tool -->
        <template v-else-if="entry.type === 'tool'">
          <span class="wp-icon">⚙</span>
          <span class="wp-text">{{ entry.text }}</span>
          <span v-if="entry.detail" class="wp-detail">{{ entry.detail }}</span>
          <span class="wp-time">{{ entry.time }}</span>
        </template>
        <!-- result -->
        <template v-else-if="entry.type === 'result'">
          <span class="wp-icon">{{ entry.icon }}</span>
          <span class="wp-text wp-result-text">{{ entry.text }}</span>
          <span class="wp-time">{{ entry.time }}</span>
          <div v-if="entry.detail" class="wp-conclusion">{{ entry.detail }}</div>
        </template>
        <!-- user / planner / log / error -->
        <template v-else>
          <span class="wp-icon" v-if="entry.icon">{{ entry.icon }}</span>
          <span class="wp-text">{{ entry.text }}</span>
          <span class="wp-time">{{ entry.time }}</span>
        </template>
      </div>
      <div v-if="timeline.length === 0" class="wp-empty">输入测试指令开始执行</div>
    </div>

    <!-- 输入区 -->
    <div class="wp-input-row">
      <el-input v-model="inputText" type="textarea" :rows="2"
                placeholder="输入测试指令，如: 检查 Settings 的 WLAN 开关是否正常"
                @keydown.enter.ctrl="$emit('run', inputText)" />
      <el-button type="primary" :loading="executing" @click="$emit('run', inputText)" style="margin-top:6px">
        {{ executing ? '执行中...' : '开始执行' }}
      </el-button>
    </div>
  </section>
</template>

<script setup>
import { ref, nextTick, watch } from 'vue'

const props = defineProps({ executing: { type: Boolean, default: false } })
defineEmits(['run'])

const timeline = ref([])
const thinkingEntry = ref(null)
const inputText = ref('')

// ── 双维度标签 ──
const _execMap = { completed: '已完成', exhausted: '步骤耗尽', error: '异常中断', cancelled: '已取消', device_offline: '设备离线' }
const _verdictMap = { passed: '通过', failed: '未通过', inconclusive: '待确认' }
function execLabel(s) { return _execMap[s] || '异常中断' }
function verdictLabel(s) { return _verdictMap[s] || '待确认' }

// ── 工具函数 ──
function now() { return new Date().toLocaleTimeString('zh-CN', { hour12: false }) }

function startThinking() {
  if (thinkingEntry.value) return
  thinkingEntry.value = { type: 'thinking', text: 'AI 思考中...', time: now(), id: Date.now() }
  timeline.value.push(thinkingEntry.value)
}

function stopThinking() {
  if (thinkingEntry.value) {
    const idx = timeline.value.indexOf(thinkingEntry.value)
    if (idx !== -1) timeline.value.splice(idx, 1)
    thinkingEntry.value = null
  }
}

function addEntry(entry) {
  stopThinking()
  timeline.value.push({ ...entry, time: now(), id: Date.now() })
  if (timeline.value.length > 500) timeline.value.shift()
}

function addTool(name, target) {
  startThinking()
  stopThinking()
  const text = target ? `${name} → ${target}` : name
  timeline.value.push({ type: 'tool', icon: '⚙', text, time: now(), id: Date.now() })
}

function finishTool(name, elapsedMs) {
  const last = [...timeline.value].reverse().find(e => e.type === 'tool')
  if (last) last.detail = elapsedMs ? `${name} ${elapsedMs}ms` : name
}

function addResult(execStatus, verdict, conclusion, verificationResults) {
  stopThinking()
  const icon = verdict === 'passed' ? '✅' : verdict === 'failed' ? '❌' : '⚠️'
  addEntry({ type: 'result', icon, text: `${execLabel(execStatus)} | ${verdictLabel(verdict)}`, detail: conclusion })
  if (verificationResults && verificationResults.length) {
    verificationResults.forEach(v =>
      addEntry({ type: 'log', text: `${v.result === 'passed' ? '✓' : '✗'} ${v.item}` })
    )
  }
}

function onToken() { startThinking() }

defineExpose({ addEntry, addTool, finishTool, addResult, onToken })

// 自动滚动
watch(() => timeline.value.length, () => {
  nextTick(() => {
    const el = document.querySelector('.wp-timeline')
    if (el) el.scrollTop = el.scrollHeight
  })
})
</script>

<style scoped>
.wp-root { display: flex; flex-direction: column; height: 100%; gap: 8px; }
.wp-timeline { flex: 1; overflow-y: auto; padding: 8px 12px; background: #fafbfc; border-radius: 8px; border: 1px solid #e4e7ed; min-height: 200px; max-height: calc(100vh - 320px); }
.wp-empty { text-align: center; color: #bbb; padding: 60px 0; font-size: 14px; }
.wp-entry { display: flex; align-items: flex-start; gap: 6px; padding: 3px 0; font-size: 13px; line-height: 1.5; }
.wp-entry.wp-tool { color: #555; }
.wp-entry.wp-result { flex-wrap: wrap; padding: 4px 0; }
.wp-entry.wp-error { color: #ef4444; }
.wp-icon { flex-shrink: 0; width: 18px; text-align: center; }
.wp-time { flex-shrink: 0; color: #bbb; font-size: 10px; margin-left: auto; min-width: 45px; text-align: right; }
.wp-text { color: #444; }
.wp-detail { color: #999; font-size: 11px; }
.wp-conclusion { width: 100%; margin-top: 4px; padding: 6px 8px; background: #fff; border-radius: 4px; font-size: 12px; color: #666; white-space: pre-wrap; word-break: break-all; }
.wp-result-text { font-weight: 600; }

/* thinking spinner */
.wp-spinner { width: 12px; height: 12px; border: 2px solid #e0e0e0; border-top-color: #409eff; border-radius: 50%; animation: spin 0.6s linear infinite; flex-shrink: 0; margin-top: 2px; }
@keyframes spin { to { transform: rotate(360deg); } }
.wp-entry.wp-thinking { color: #409eff; }

/* 输入区 */
.wp-input-row { padding: 4px 0; }
</style>

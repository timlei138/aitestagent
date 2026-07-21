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
          <span class="wp-icon-wrap wp-icon-tool">⚙</span>
          <span v-if="entry.intent" class="wp-intent">AI意图: {{ entry.intent }}</span>
          <span class="wp-text">{{ entry.text }}</span>
          <span v-if="entry.detail" class="wp-detail">{{ entry.detail }}</span>
          <span class="wp-time">{{ entry.time }}</span>
        </template>
        <!-- result -->
        <template v-else-if="entry.type === 'result'">
          <span class="wp-icon-wrap wp-icon-result">{{ entry.icon }}</span>
          <span class="wp-text wp-result-text">{{ entry.text }}</span>
          <span class="wp-time">{{ entry.time }}</span>
          <div v-if="entry.detail" class="wp-conclusion">{{ entry.detail }}</div>
        </template>
        <!-- user / planner / log / error -->
        <template v-else>
          <span class="wp-icon-wrap" v-if="entry.icon">{{ entry.icon }}</span>
          <span class="wp-text">{{ entry.text }}</span>
          <span class="wp-time">{{ entry.time }}</span>
        </template>
      </div>
      <div v-if="timeline.length === 0" class="wp-empty">
        <div class="wp-empty-icon">◇</div>
        <div class="wp-empty-text">输入测试指令开始执行</div>
      </div>
    </div>

    <!-- 输入区 -->
    <div class="wp-input-area">
      <div class="wp-input-wrap">
        <el-input v-model="inputText" type="textarea" :rows="2"
                  placeholder="输入测试指令，如: 检查 Settings 的 WLAN 开关是否正常"
                  class="wp-textarea"
                  @keydown.enter.ctrl="$emit('run', inputText)" />
        <div class="wp-input-actions">
          <span class="wp-input-hint">Ctrl + Enter 发送</span>
          <div class="wp-input-buttons">
            <el-button
              v-if="executing"
              type="danger"
              :loading="stopping"
              @click="$emit('stop')"
              round
            >
              {{ stopping ? '正在停止...' : '停止运行' }}
            </el-button>
            <el-button
              v-else
              type="primary"
              @click="$emit('run', inputText); inputText = ''"
              round
            >
              开始执行
            </el-button>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup>
import { ref, nextTick, watch } from 'vue'

const props = defineProps({
  executing: { type: Boolean, default: false },
  stopping: { type: Boolean, default: false },
})
defineEmits(['run', 'stop'])

const timeline = ref([])
const thinkingEntry = ref(null)
const inputText = ref('')

// ── 双维度标签 ──
const _execMap = { completed: '已完成', exhausted: '步骤耗尽', error: '异常中断', cancelled: '已取消', device_offline: '设备离线' }
const _verdictMap = { passed: '通过', failed: '未通过', inconclusive: '待人工复核' }
function execLabel(s) { return _execMap[s] || '异常中断' }
function verdictLabel(s) { return _verdictMap[s] || '待人工复核' }

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

function _stripPrefix(s) {
  return String(s || '').replace(/^(?:#{1,3}\s*)?(?:DONE|ABORT)\s*[:：]\s*/im, '').trim()
}

function _shortIntentText(text) {
  const cleaned = _stripPrefix(text).replace(/\s+/g, ' ').trim()
  if (!cleaned) return ''
  return cleaned.length > 80 ? cleaned.slice(0, 80) + '…' : cleaned
}

function addTool(name, target, intentText = '') {
  console.log('[WP] addTool:', name, target)
  startThinking()
  stopThinking()
  const text = target ? `${name} → ${target}` : name
  timeline.value.push({
    type: 'tool',
    icon: '⚙',
    text,
    intent: _shortIntentText(intentText),
    time: now(),
    id: Date.now(),
  })
}

function finishTool(name, elapsedMs) {
  console.log('[WP] finishTool:', name, elapsedMs)
  const last = [...timeline.value].reverse().find(e => e.type === 'tool')
  if (last && elapsedMs) last.detail = `${name} ${elapsedMs}ms`
}

function addResult(execStatus, verdict, conclusion, verificationResults) {
  console.log('[WP] addResult:', execStatus, verdict)
  stopThinking()
  const icon = verdict === 'passed' ? '✅' : verdict === 'failed' ? '❌' : '⚠️'
  addEntry({ type: 'result', icon, text: `${execLabel(execStatus)} | ${verdictLabel(verdict)}`, detail: conclusion })
  if (verificationResults && verificationResults.length) {
    verificationResults.forEach(v => {
      const needsReview = v.review_required || v.result === 'unknown'
      const icon = v.result === 'passed' ? '✓' : v.result === 'failed' ? '✗' : '⚠️'
      const suffix = needsReview ? '（需人工复核）' : ''
      addEntry({ type: 'log', text: `${icon} ${v.item}${suffix}` })
    })
  }
}

function onToken() { startThinking() }
function addEntry(entry) {
  console.log('[WP] addEntry:', entry.type, entry.text?.substring(0, 40))
  stopThinking()
  timeline.value.push({ ...entry, time: now(), id: Date.now() })
  if (timeline.value.length > 500) timeline.value.shift()
}

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
.wp-root {
  display: flex;
  flex-direction: column;
  height: 100%;
  gap: 12px;
}

/* ── Timeline ── */
.wp-timeline {
  flex: 1;
  overflow-y: auto;
  padding: 16px 20px;
  background: var(--bg-timeline, #fafbfc);
  border-radius: var(--radius-lg, 16px);
  border: 1px solid var(--line, #e8eaed);
  min-height: 200px;
  max-height: calc(100vh - 260px);
}
.wp-timeline::-webkit-scrollbar { width: 4px; }
.wp-timeline::-webkit-scrollbar-thumb { background: #d4d6db; border-radius: 4px; }

/* ── Empty State ── */
.wp-empty {
  text-align: center;
  padding: 80px 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}
.wp-empty-icon {
  font-size: 32px;
  color: var(--accent, #6366f1);
  opacity: .35;
}
.wp-empty-text {
  color: var(--text-muted, #9aa0a6);
  font-size: 14px;
}

/* ── Entry ── */
.wp-entry {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 6px 0;
  font-size: 13px;
  line-height: 1.6;
  border-bottom: 1px solid var(--line-light, #f0f1f3);
}
.wp-entry:last-child { border-bottom: none; }

.wp-entry.wp-tool { color: var(--text-secondary, #5f6368); }
.wp-entry.wp-result { flex-wrap: wrap; padding: 8px 0; }
.wp-entry.wp-error { color: var(--danger, #ef4444); }
.wp-entry.wp-planner { color: var(--accent, #6366f1); }
.wp-entry.wp-user { color: var(--text-primary, #1a1d23); font-weight: 500; }

/* ── Icon ── */
.wp-icon-wrap {
  flex-shrink: 0;
  width: 22px;
  height: 22px;
  display: grid;
  place-items: center;
  font-size: 13px;
  border-radius: 6px;
  background: var(--line-light, #f0f1f3);
}
.wp-icon-tool { background: #eef2ff; }
.wp-icon-result { background: transparent; font-size: 15px; }

.wp-time {
  flex-shrink: 0;
  color: var(--text-muted, #9aa0a6);
  font-size: 10px;
  margin-left: auto;
  min-width: 50px;
  text-align: right;
  padding-top: 2px;
}
.wp-text { color: var(--text-secondary, #5f6368); }
.wp-entry.wp-user .wp-text { color: var(--text-primary, #1a1d23); }
.wp-intent { color: var(--text-primary, #1a1d23); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 42%; }
.wp-detail { color: var(--text-muted, #9aa0a6); font-size: 11px; }
.wp-result-text { font-weight: 600; }

/* ── Conclusion ── */
.wp-conclusion {
  width: 100%;
  margin-top: 6px;
  padding: 10px 12px;
  background: #fff;
  border-radius: var(--radius-sm, 8px);
  font-size: 12px;
  color: var(--text-secondary, #5f6368);
  white-space: pre-wrap;
  word-break: break-all;
  border: 1px solid var(--line-light, #f0f1f3);
  line-height: 1.6;
}

/* ── Thinking Spinner ── */
.wp-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid #e0e2e8;
  border-top-color: var(--accent, #6366f1);
  border-radius: 50%;
  animation: spin 0.7s linear infinite;
  flex-shrink: 0;
  margin-top: 2px;
}
@keyframes spin { to { transform: rotate(360deg); } }
.wp-entry.wp-thinking { color: var(--accent, #6366f1); }
.wp-entry.wp-thinking .wp-text { color: var(--accent, #6366f1); font-weight: 500; }

/* ── Input Area ── */
.wp-input-area {
  padding: 4px 0;
  flex-shrink: 0;
}
.wp-input-wrap {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.wp-input-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.wp-input-buttons {
  display: flex;
  gap: 8px;
  align-items: center;
}
.wp-input-hint {
  font-size: 11px;
  color: var(--text-muted, #9aa0a6);
}

/* ── Textarea focus glow ── */
.wp-textarea :deep(.el-textarea__inner:focus) {
  box-shadow: var(--shadow-focus, 0 0 0 3px rgba(99,102,241,.18));
  border-color: var(--accent, #6366f1);
}
.wp-textarea :deep(.el-textarea__inner) {
  border-radius: var(--radius-md, 12px) !important;
  border: 1.5px solid var(--line, #e8eaed);
  padding: 12px 16px;
  font-size: 14px;
  line-height: 1.6;
  resize: none;
  transition: border-color var(--duration, .2s) var(--ease, ease),
              box-shadow var(--duration, .2s) var(--ease, ease);
}
</style>

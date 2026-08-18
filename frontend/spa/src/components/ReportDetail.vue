<template>
  <div v-if="report" class="report-detail">
    <div class="rd-banner" :class="'rd-' + (report.verdict || 'inconclusive')">
      <div class="rd-banner-icon">{{ bannerIcon }}</div>
      <div class="rd-banner-body">
        <div class="rd-banner-title">{{ bannerTitle }}</div>
        <div class="rd-banner-meta">
          <span>模式: {{ report.execution_mode || 'explore' }}</span>
          <span>阶段: {{ report.lifecycle_state || 'Terminal' }}</span>
          <span>{{ (report.created_at || '').replace('T', ' ').substring(0, 19) }}</span>
        </div>
      </div>
    </div>

    <div class="rd-metrics">
      <div class="rd-metric-item">
        <span class="rd-metric-label">行动事件</span>
        <b class="rd-metric-value">{{ actions.length }}</b>
      </div>
      <div class="rd-metric-item">
        <span class="rd-metric-label">证据事件</span>
        <b class="rd-metric-value">{{ evidence.length }}</b>
      </div>
      <div class="rd-metric-item">
        <span class="rd-metric-label">LLM 调用</span>
        <b class="rd-metric-value">{{ report.llm_call_count || 0 }}</b>
      </div>
      <div class="rd-metric-item">
        <span class="rd-metric-label">总体耗时</span>
        <b class="rd-metric-value">{{ formatDuration(report.duration_seconds) }}</b>
      </div>
      <div class="rd-metric-item" v-if="tokenTotal">
        <span class="rd-metric-label">Token 消耗</span>
        <b class="rd-metric-value" :title="tokenTooltip">{{ tokenTotal }}</b>
      </div>
    </div>

    <!-- 请求文本 -->
    <div class="rd-request" v-if="report.user_request">{{ report.user_request }}</div>

    <div v-if="evidence.length" class="rd-verification">
      <div class="rd-section-title">验证证据</div>
      <div v-for="(v, i) in evidence" :key="v.evidence_id || i" class="rd-verify-item">
        <span class="rd-verify-icon" :class="evidenceClass(v.status)">{{ evidenceIcon(v.status) }}</span>
        <div class="rd-verify-main">
          <span class="rd-verify-text">{{ clauseText(v) }} · {{ channelLabel(v.channel) }}</span>
          <div v-if="v.fact && Object.keys(v.fact).length" class="rd-verify-reason">{{ JSON.stringify(v.fact) }}</div>
        </div>
      </div>
    </div>

    <div v-if="report.terminal_reason" class="rd-conclusion">
      <div class="rd-section-title">终止理由</div>
      <pre>{{ report.terminal_reason }}</pre>
    </div>

    <div v-if="modeTransitions.length" class="rd-conclusion">
      <div class="rd-section-title">模式降级</div>
      <pre v-for="transition in modeTransitions" :key="transition.created_at">{{ transition.from_mode }} -> {{ transition.to_mode }}: {{ transition.reason }}</pre>
    </div>

    <details class="rd-steps-details">
      <summary class="rd-section-title">行动详情 · {{ actions.length }} 项</summary>
      <div class="rd-steps">
        <div class="rd-step" v-for="s in actions" :key="`${s.action_index}-${s.created_at}`"
             :class="{ 'rd-step-fail': s.status === 'ERROR', 'rd-step-done': s.status === 'OK' }">
          <div class="rd-step-head">
            <span class="rd-step-idx">{{ s.action_index }}</span>
            <code class="rd-step-action">{{ s.tool_name }}</code>
            <span class="rd-step-target">{{ s.execution_mode }}</span>
            <span class="rd-step-badge" :class="s.status === 'OK' ? 'done' : 'fail'">{{ s.status }}</span>
            <img v-if="s.screenshot" class="step-shot" :src="screenshotUrl(s.screenshot)" @click="openLightbox(s.screenshot)" />
          </div>
          <div v-if="s.intent" class="rd-step-intent">{{ s.intent }}</div>
          <div v-if="s.resolved_locator && Object.keys(s.resolved_locator).length" class="rd-step-pages">定位：{{ JSON.stringify(s.resolved_locator) }}</div>
          <div v-if="s.tool_input && Object.keys(s.tool_input).length" class="rd-step-obs">输入：{{ JSON.stringify(s.tool_input) }}</div>
        </div>
      </div>
    </details>
  </div>

  <!-- 截图放大 lightbox -->
  <div v-if="lightboxUrl" class="rd-lightbox" @click.self="lightboxUrl = ''">
    <img :src="lightboxUrl" />
  </div>

  <div v-else class="rd-empty">加载中...</div>
</template>

<script setup>
import { computed, ref } from 'vue'

const props = defineProps({ report: { type: Object, default: null } })
const actions = computed(() => props.report?.actions || [])
const evidence = computed(() => props.report?.evidence || [])
const modeTransitions = computed(() => props.report?.mode_transitions || [])

const tokenUsage = computed(() => props.report?.token_usage || {})
const tokenTotal = computed(() => {
  const t = tokenUsage.value?.total_tokens || 0
  return t ? t.toLocaleString() : ''
})
const tokenTooltip = computed(() => {
  const t = tokenUsage.value
  if (!t) return ''
  return `输入: ${t.input_tokens || 0}\n输出: ${t.output_tokens || 0}\n缓存输入: ${t.cached_input_tokens || 0}`
})
function formatDuration(sec) {
  const s = Number(sec || 0)
  if (s < 60) return `${s.toFixed(1)}s`
  const m = Math.floor(s / 60)
  const r = (s % 60).toFixed(0)
  return `${m}m ${r.padStart(2, '0')}s`
}
const lightboxUrl = ref('')
function openLightbox(path) {
  if (!path) return
  lightboxUrl.value = screenshotUrl(path)
}

const bannerIcon = computed(() => {
  if (!props.report) return '⏳'
  if (props.report.verdict === 'passed') return '✅'
  if (props.report.verdict === 'failed') return '❌'
  return '⚠️'
})
const bannerTitle = computed(() => {
  if (!props.report) return ''
  const v = props.report.verdict || 'inconclusive'
  const m = { passed: '测试通过', failed: '测试未通过', inconclusive: '待人工复核' }
  return m[v] || v
})

function evidenceIcon(status) { return status === 'PASS' ? '✓' : status === 'FAIL' ? '✗' : '?' }
function evidenceClass(status) { return status === 'PASS' ? 'passed' : status === 'FAIL' ? 'failed' : 'unknown' }

const channelLabels = {
  ui_text: '页面文本',
  element_state: '元素状态',
  vision_verify: '视觉确认',
  click_and_check: '点击验证',
  page_state: '页面状态',
  behavior_effect: '行为效果',
}
function channelLabel(channel) { return channelLabels[channel] || channel || '未知通道' }

// 用 verification_key + clause_id 反查 contract，显示可读的 statement/claim，而非内部 key。
function clauseText(v) {
  const verifications = props.report?.verification_contract?.verifications || []
  const ver = verifications.find(x => x.key === v.verification_key)
  if (!ver) return v.verification_key
  const clause = (ver.clauses || []).find(c => c.id === v.clause_id)
  if (clause && clause.claim) return clause.claim
  return ver.statement || v.verification_key
}

// 截图相对路径拼成 /storage/ 静态资源 URL。
function screenshotUrl(path) {
  if (!path) return ''
  const p = String(path).replace(/\\/g, '/').replace(/^\/+/, '')
  return `/storage/${p}`
}
</script>

<style scoped>
/* ── 横幅 ── */
.rd-banner { display: flex; align-items: center; gap: 16px; padding: 20px 24px; border-radius: var(--radius-lg); margin-bottom: 16px; }
.rd-passed    { background: linear-gradient(135deg, #ecfdf5, #d1fae5); border: 1.5px solid #86efac; }
.rd-failed    { background: linear-gradient(135deg, #fef2f2, #fee2e2); border: 1.5px solid #fca5a5; }
.rd-inconclusive { background: #fefce8; border: 1.5px solid #fde68a; }
.rd-banner-icon { font-size: 40px; }
.rd-banner-title { font-size: 22px; font-weight: 700; color: var(--text-primary); }
.rd-banner-meta { display: flex; gap: 16px; font-size: 13px; color: var(--text-secondary); margin-top: 4px; }
.rd-metrics { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin-bottom: 12px; }
.rd-metric-item { background: #fafbfc; border: 1px solid var(--line-light); border-radius: var(--radius-sm); padding: 8px 10px; display: flex; align-items: center; justify-content: space-between; }
.rd-metric-label { font-size: 12px; color: var(--text-muted); }
.rd-metric-value { font-size: 13px; color: var(--text-primary); }

/* ── 请求 ── */
.rd-request { padding: 12px 16px; background: var(--accent-light); border-radius: var(--radius-sm); font-size: 14px; color: var(--accent); margin-bottom: 12px; border-left: 3px solid var(--accent); }

/* ── 验证 ── */
.rd-verification { margin-bottom: 12px; }
.rd-section-title { font-size: 13px; font-weight: 700; color: var(--text-secondary); cursor: pointer; padding: 6px 0; border-bottom: 1px solid var(--line-light); margin-bottom: 8px; user-select: none; }
.rd-verify-item { display: flex; align-items: flex-start; gap: 8px; padding: 4px 0; font-size: 13px; }
.rd-verify-icon { font-weight: 700; width: 22px; text-align: center; }
.rd-verify-icon.passed { color: var(--success); } .rd-verify-icon.failed { color: var(--danger); } .rd-verify-icon.unknown { color: var(--warning); }
.rd-verify-text { flex: 1; color: var(--text-secondary); }
.rd-review-badge { align-self: flex-start; width: fit-content; padding: 1px 6px; border-radius: var(--radius-xs); background: #fef3c7; color: #92400e; font-size: 11px; font-weight: 600; }
.rd-verify-main { flex: 1; display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.rd-verify-reason { color: var(--text-muted); font-size: 12px; white-space: pre-wrap; word-break: break-word; }
.verify-shot { width: 48px; height: 36px; border-radius: var(--radius-xs); cursor: pointer; object-fit: cover; border: 1px solid var(--line); }

/* ── 结论 ── */
.rd-conclusion { margin-bottom: 12px; padding: 14px; background: #fafbfc; border-radius: var(--radius-sm); }
.rd-conclusion pre { font-size: 12px; white-space: pre-wrap; margin: 0; color: var(--text-secondary); }

/* ── 统计内联 ── */
.rd-stat b { font-size: 14px; margin: 0 2px; }
.rd-stat.pass { color: var(--success); } .rd-stat.fail { color: var(--danger); }

/* ── 步骤 ── */
.rd-steps-details { }
.rd-steps { display: flex; flex-direction: column; gap: 4px; margin-top: 6px; }
.rd-step { padding: 8px 12px; border-radius: var(--radius-sm); background: #fafbfc; border-left: 3px solid var(--line); }
.rd-step-fail { border-left-color: var(--danger); background: #fef2f2; }
.rd-step-done { border-left-color: var(--success); background: #f0fdf4; }
.rd-step-head { display: flex; align-items: center; gap: 6px; font-size: 13px; }
.rd-step-idx { color: var(--text-muted); min-width: 20px; font-size: 11px; }
.rd-step-action { font-weight: 600; color: var(--text-secondary); background: var(--bg-tag); padding: 2px 8px; border-radius: var(--radius-xs); font-size: 12px; font-family: 'JetBrains Mono', monospace; }
.rd-step-target { color: var(--text-muted); font-size: 12px; }
.rd-step-time { font-size: 11px; color: var(--text-muted); margin-left: auto; }
.rd-step-badge { font-size: 10px; padding: 1px 6px; border-radius: var(--radius-xs); font-weight: 600; }
.rd-step-badge.fail { background: #fecaca; color: #dc2626; }
.rd-step-badge.done { background: #bbf7d0; color: #16a34a; }
.rd-step-pages { font-size: 11px; color: var(--text-muted); margin-top: 2px; }
.rd-step-intent { font-size: 12px; color: var(--text-primary); margin-top: 4px; padding: 6px 8px; background: #f6f8fa; border-radius: 6px; border: 1px dashed var(--line-light); white-space: pre-wrap; word-break: break-word; }
.rd-step-obs { font-size: 12px; color: var(--text-secondary); margin-top: 4px; padding: 8px 10px; background: #fff; border-radius: var(--radius-xs); white-space: pre-wrap; word-break: break-all; max-height: 120px; overflow-y: auto; border: 1px solid var(--line-light); }

/* ── 步骤截图 ── */
.step-shot { width: 40px; height: 30px; border-radius: 4px; cursor: pointer; object-fit: cover; border: 1px solid var(--line); margin-left: 8px; }

/* ── 截图 lightbox ── */
.rd-lightbox { position: fixed; inset: 0; background: rgba(0,0,0,0.85); display: flex; align-items: center; justify-content: center; z-index: 1000; padding: 24px; }
.rd-lightbox img { max-width: 90vw; max-height: 90vh; border-radius: var(--radius-sm); box-shadow: 0 20px 60px rgba(0,0,0,0.4); }
</style>

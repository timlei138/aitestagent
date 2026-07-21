<template>
  <div v-if="report" class="report-detail">
    <!-- 顶部：大号通过/失败横幅 -->
    <div class="rd-banner" :class="'rd-' + (report.test_verdict || 'inconclusive')">
      <div class="rd-banner-icon">{{ bannerIcon }}</div>
      <div class="rd-banner-body">
        <div class="rd-banner-title">{{ bannerTitle }}</div>
        <div class="rd-banner-meta">
          <span>执行: {{ execStatusLabel(report.execution_status) }}</span>
          <span>耗时: {{ report.duration_seconds || 0 }}s</span>
          <span>{{ (report.created_at || '').replace('T', ' ').substring(0, 19) }}</span>
        </div>
      </div>
    </div>

    <div class="rd-metrics">
      <div class="rd-metric-item">
        <span class="rd-metric-label">LLM调用</span>
        <b class="rd-metric-value">{{ Number(report.llm_call_count || 0) }}</b>
      </div>
      <div class="rd-metric-item">
        <span class="rd-metric-label">点击 (精确/模糊/歧义)</span>
        <b class="rd-metric-value">{{ Number(report.click_count || 0) }} / {{ Number(report.fuzzy_click_count || 0) }} / {{ Number(report.ambiguous_count || 0) }}</b>
      </div>
      <div class="rd-metric-item">
        <span class="rd-metric-label">精确点击率</span>
        <b class="rd-metric-value">{{ fmtRate(report.exact_click_rate) }}</b>
      </div>
      <div class="rd-metric-item">
        <span class="rd-metric-label">模糊点击率</span>
        <b class="rd-metric-value">{{ fmtRate(report.fuzzy_click_rate) }}</b>
      </div>
      <div class="rd-metric-item">
        <span class="rd-metric-label">RAG查询</span>
        <b class="rd-metric-value">{{ Number(report.rag_query_count || 0) }}</b>
      </div>
      <div class="rd-metric-item">
        <span class="rd-metric-label">RAG同App率</span>
        <b class="rd-metric-value">{{ fmtRate(report.rag_same_app_ratio) }}</b>
      </div>
      <div class="rd-metric-item">
        <span class="rd-metric-label">RAG跨App</span>
        <b class="rd-metric-value">{{ Number(report.rag_cross_app_used_count || 0) }}</b>
      </div>
      <div class="rd-metric-item">
        <span class="rd-metric-label">Token 消耗 (入/出/总)</span>
        <b class="rd-metric-value">{{ fmtTokens(report.input_tokens) }} / {{ fmtTokens(report.output_tokens) }} / {{ fmtTokens(report.total_tokens) }}</b>
      </div>
      <div class="rd-metric-item">
        <span class="rd-metric-label">Token 缓存命中</span>
        <b class="rd-metric-value">{{ fmtTokens(report.cached_input_tokens) }}</b>
      </div>
    </div>

    <!-- 请求文本 -->
    <div class="rd-request" v-if="report.user_request">{{ report.user_request }}</div>

    <!-- 验证清单 -->
    <div v-if="report.verification_results && report.verification_results.length" class="rd-verification">
      <div class="rd-section-title">验证清单</div>
      <div v-for="(v, i) in report.verification_results" :key="v.screenshot || i" class="rd-verify-item">
        <span class="rd-verify-icon" :class="v.result">{{ v.result === 'passed' ? '✓' : v.result === 'failed' ? '✗' : '?' }}</span>
        <div class="rd-verify-main">
          <span class="rd-verify-text">{{ v.item }}</span>
          <span v-if="v.review_required || v.result === 'unknown'" class="rd-review-badge">需人工复核</span>
          <div v-if="v.detail" class="rd-verify-reason">理由：{{ v.detail }}</div>
        </div>
        <el-image v-if="v.screenshot" :src="shotUrl(v.screenshot, i)" :preview-src-list="[shotUrl(v.screenshot, i)]"
                  fit="cover" class="verify-shot" title="点击查看大图" />
      </div>
    </div>

    <!-- 执行结论（验证清单下方，剥离 DONE/ABORT 前缀） -->
    <div v-if="cleanConclusion" class="rd-conclusion">
      <div class="rd-section-title">执行结论</div>
      <pre>{{ cleanConclusion }}</pre>
    </div>

    <!-- 步骤统计 + 折叠步骤 -->
    <details class="rd-steps-details">
      <summary class="rd-section-title">执行详情 · {{ report.total_steps || 0 }} 步 ·
        <span class="rd-stat pass">✓ {{ report.pass_count || 0 }}</span>
        <span class="rd-stat fail">✗ {{ report.fail_count || 0 }}</span>
      </summary>
      <div class="rd-steps">
        <div class="rd-step" v-for="s in (report.steps || [])" :key="s.index"
             :class="{ 'rd-step-fail': s.status === 'fail', 'rd-step-done': s.status === 'success' }">
          <div class="rd-step-head">
            <span class="rd-step-idx">{{ s.index }}</span>
            <code class="rd-step-action">{{ s.action_type }}</code>
            <span v-if="s.target" class="rd-step-target">→ {{ s.target }}</span>
            <el-image v-if="s.screenshot_path"
                      :src="shotUrl(s.screenshot_path, s.index)"
                      :preview-src-list="[shotUrl(s.screenshot_path, s.index)]"
                      fit="cover" class="step-shot" title="点击查看大图" />
            <span v-if="s.duration_ms" class="rd-step-time">{{ fmtDuration(s.duration_ms) }}</span>
            <span v-if="s.status === 'fail'" class="rd-step-badge fail">失败</span>
            <span v-if="s.status === 'success'" class="rd-step-badge done">完成</span>
          </div>
          <div v-if="s.page_from || s.page_to" class="rd-step-pages">{{ s.page_from || '?' }} → {{ s.page_to || '?' }}</div>
          <div v-if="stepIntent(s)" class="rd-step-intent">AI意图：{{ stepIntent(s) }}</div>
          <div v-if="s.observation" class="rd-step-obs">{{ stripDONE(s.observation) }}</div>
        </div>
      </div>
    </details>
  </div>

  <div v-else class="rd-empty">加载中...</div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({ report: { type: Object, default: null } })

const bannerIcon = computed(() => {
  if (!props.report) return '⏳'
  if (props.report.test_verdict === 'passed') return '✅'
  if (props.report.test_verdict === 'failed') return '❌'
  return '⚠️'
})
const bannerTitle = computed(() => {
  if (!props.report) return ''
  const v = props.report.test_verdict || 'inconclusive'
  const m = { passed: '测试通过', failed: '测试未通过', inconclusive: '待人工复核' }
  return m[v] || v
})

// 剥离 DONE:/ABORT: 前缀的结论文本
const cleanConclusion = computed(() => {
  const c = (props.report?.conclusion || '').trim()
  if (!c) return ''
  // 去掉 DONE: / ABORT: 前缀（含 ## 变体）
  return c.replace(/^(?:#{1,3}\s*)?(?:DONE|ABORT)\s*[:：]\s*/im, '').trim()
})

function stripDONE(s) {
  return (s || '').replace(/^(?:#{1,3}\s*)?(?:DONE|ABORT)\s*[:：]\s*/im, '').trim()
}

function stepIntent(step) {
  const t = String(step?.intent_text || step?.intent || '').trim()
  return t ? stripDONE(t) : ''
}

const execStatusMap = {
  completed: { label: '已完成' }, exhausted: { label: '步骤耗尽' },
  error: { label: '异常中断' }, cancelled: { label: '已取消' },
  device_offline: { label: '设备离线' },
}
function execStatusLabel(s) { return (execStatusMap[s] || execStatusMap.error).label }

function fmtRate(v) {
  const n = Number(v || 0)
  return (n * 100).toFixed(2) + '%'
}

function fmtTokens(v) {
  const n = Number(v || 0)
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + 'M'
  if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K'
  return String(n)
}

function fmtDuration(ms) {
  if (!ms || ms <= 0) return ''
  if (ms < 1000) return ms + 'ms'
  if (ms < 60000) return (ms / 1000).toFixed(1) + 's'
  const m = Math.floor(ms / 60000)
  const s = Math.round((ms % 60000) / 1000)
  return s > 0 ? `${m}m${s}s` : `${m}m`
}

function shotUrl(path, index) {
  let normalized = String(path || '').replace(/\\/g, '/').replace(/^\/+/, '')
  // 确保路径以 storage/ 开头以匹配服务器的 /storage 挂载点
  if (!normalized.startsWith('storage/')) {
    normalized = 'storage/' + normalized
  }
  return `/${normalized}?v=${props.report?.id || 'report'}_${index}`
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
</style>

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

    <!-- 请求文本 -->
    <div class="rd-request" v-if="report.user_request">{{ report.user_request }}</div>

    <!-- 验证清单 -->
    <div v-if="report.verification_results && report.verification_results.length" class="rd-verification">
      <div class="rd-section-title">验证清单</div>
      <div v-for="(v, i) in report.verification_results" :key="i" class="rd-verify-item">
        <span class="rd-verify-icon" :class="v.result">{{ v.result === 'passed' ? '✓' : v.result === 'failed' ? '✗' : '?' }}</span>
        <span class="rd-verify-text">{{ v.item }}</span>
        <el-image v-if="v.screenshot" :src="'/' + v.screenshot" :preview-src-list="['/' + v.screenshot]"
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
            <span v-if="s.duration_ms" class="rd-step-time">{{ fmtDuration(s.duration_ms) }}</span>
            <span v-if="s.status === 'fail'" class="rd-step-badge fail">失败</span>
            <span v-if="s.status === 'success'" class="rd-step-badge done">完成</span>
          </div>
          <div v-if="s.page_from || s.page_to" class="rd-step-pages">{{ s.page_from || '?' }} → {{ s.page_to || '?' }}</div>
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
  const m = { passed: '测试通过', failed: '测试未通过', inconclusive: '无法判定' }
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

const execStatusMap = {
  completed: { label: '已完成' }, exhausted: { label: '步骤耗尽' },
  error: { label: '异常中断' }, cancelled: { label: '已取消' },
  device_offline: { label: '设备离线' },
}
function execStatusLabel(s) { return (execStatusMap[s] || execStatusMap.error).label }

function fmtDuration(ms) {
  if (!ms || ms <= 0) return ''
  if (ms < 1000) return ms + 'ms'
  if (ms < 60000) return (ms / 1000).toFixed(1) + 's'
  const m = Math.floor(ms / 60000)
  const s = Math.round((ms % 60000) / 1000)
  return s > 0 ? `${m}m${s}s` : `${m}m`
}
</script>

<style scoped>
/* ── 横幅 ── */
.rd-banner { display: flex; align-items: center; gap: 16px; padding: 20px 24px; border-radius: 12px; margin-bottom: 16px; }
.rd-passed    { background: linear-gradient(135deg, #ecfdf5, #d1fae5); border: 1.5px solid #86efac; }
.rd-failed    { background: linear-gradient(135deg, #fef2f2, #fee2e2); border: 1.5px solid #fca5a5; }
.rd-inconclusive { background: #fefce8; border: 1.5px solid #fde68a; }
.rd-banner-icon { font-size: 40px; }
.rd-banner-title { font-size: 22px; font-weight: 700; color: #1a1a2e; }
.rd-banner-meta { display: flex; gap: 16px; font-size: 13px; color: #666; margin-top: 4px; }

/* ── 请求 ── */
.rd-request { padding: 10px 14px; background: #f0f5ff; border-radius: 8px; font-size: 14px; color: #1e40af; margin-bottom: 12px; border-left: 3px solid #409eff; }

/* ── 验证 ── */
.rd-verification { margin-bottom: 12px; }
.rd-section-title { font-size: 13px; font-weight: 700; color: #555; cursor: pointer; padding: 6px 0; border-bottom: 1px solid #eee; margin-bottom: 8px; user-select: none; }
.rd-verify-item { display: flex; align-items: center; gap: 8px; padding: 4px 0; font-size: 13px; }
.rd-verify-icon { font-weight: 700; width: 22px; text-align: center; }
.rd-verify-icon.passed { color: #22c55e; } .rd-verify-icon.failed { color: #ef4444; } .rd-verify-icon.unknown { color: #f59e0b; }
.rd-verify-text { flex: 1; color: #444; }
.verify-shot { width: 48px; height: 36px; border-radius: 4px; cursor: pointer; object-fit: cover; border: 1px solid #ddd; }

/* ── 结论 ── */
.rd-conclusion { margin-bottom: 12px; padding: 12px; background: #f5f7fa; border-radius: 8px; }
.rd-conclusion pre { font-size: 12px; white-space: pre-wrap; margin: 0; color: #555; }

/* ── 统计内联 ── */
.rd-stat b { font-size: 14px; margin: 0 2px; }
.rd-stat.pass { color: #22c55e; } .rd-stat.fail { color: #ef4444; }

/* ── 步骤 ── */
.rd-steps-details { }
.rd-steps { display: flex; flex-direction: column; gap: 4px; margin-top: 6px; }
.rd-step { padding: 7px 10px; border-radius: 6px; background: #fafafa; border-left: 3px solid #ddd; }
.rd-step-fail { border-left-color: #ef4444; background: #fef2f2; }
.rd-step-done { border-left-color: #22c55e; background: #f0fdf4; }
.rd-step-head { display: flex; align-items: center; gap: 6px; font-size: 13px; }
.rd-step-idx { color: #999; min-width: 20px; font-size: 11px; }
.rd-step-action { font-weight: 600; color: #555; background: #eee; padding: 1px 6px; border-radius: 4px; font-size: 12px; }
.rd-step-target { color: #888; font-size: 12px; }
.rd-step-time { font-size: 11px; color: #aaa; margin-left: auto; }
.rd-step-badge { font-size: 10px; padding: 1px 6px; border-radius: 4px; font-weight: 600; }
.rd-step-badge.fail { background: #fecaca; color: #dc2626; }
.rd-step-badge.done { background: #bbf7d0; color: #16a34a; }
.rd-step-pages { font-size: 11px; color: #999; margin-top: 2px; }
.rd-step-obs { font-size: 12px; color: #666; margin-top: 4px; padding: 6px 8px; background: #fff; border-radius: 4px; white-space: pre-wrap; word-break: break-all; max-height: 120px; overflow-y: auto; }
</style>

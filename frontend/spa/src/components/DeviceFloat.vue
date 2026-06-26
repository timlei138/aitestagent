<template>
  <transition name="floatwin">
    <div v-if="visible" class="device-float-win"
         :style="{ left: x + 'px', top: y + 'px', width: w + 'px' }" ref="winRef">
      <div class="float-win-header" @mousedown="onDragStart">
        <span class="float-win-title">📱 设备投屏</span>
        <div class="float-win-actions">
          <el-switch v-model="showOverlay" size="small" active-text="元素" @change="drawOverlay" />
          <el-button size="small" text @click="refresh(true)" :disabled="!online" style="margin-left:6px;color:#fff">刷新</el-button>
          <button class="float-win-close" @click="$emit('close')">×</button>
        </div>
      </div>
      <div class="preview" ref="previewRef" @mousemove="onMouseMove" @mouseleave="hoveredEl = null">
        <div v-if="!online" class="device-offline-mask">
          <div class="device-offline-content">
            <div class="device-offline-icon">📱</div>
            <div class="device-offline-title">Android 设备未连接</div>
            <div class="device-offline-desc">请检查 USB/ADB 连接</div>
          </div>
        </div>
        <canvas v-if="image" ref="cv" class="preview-canvas" />
        <img v-if="image" ref="img" :src="'data:image/png;base64,' + image" class="preview-img" @load="drawOverlay" />
        <div v-if="hoveredEl" class="element-tooltip" :style="{ left: tipX + 'px', top: tipY + 'px' }">
          <div class="et-row et-label">{{ hoveredEl.label || '(无标签)' }}</div>
          <div class="et-row" v-if="hoveredEl.text"><span class="et-k">text</span><span class="et-v">{{ hoveredEl.text }}</span></div>
          <div class="et-row" v-if="hoveredEl.content_desc"><span class="et-k">desc</span><span class="et-v">{{ hoveredEl.content_desc }}</span></div>
          <div class="et-row"><span class="et-k">rid</span><span class="et-v">{{ hoveredEl.resource_id || '-' }}</span></div>
          <div class="et-row"><span class="et-k">class</span><span class="et-v">{{ hoveredEl.class_name || '-' }}</span></div>
          <div class="et-row"><span class="et-k">role</span><span class="et-v et-badge">{{ hoveredEl.role || '-' }}</span></div>
          <div class="et-row"><span class="et-k">bounds</span><span class="et-v">{{ hoveredEl.bounds ? hoveredEl.bounds.join(', ') : '-' }}</span></div>
          <div class="et-row et-flags">
            <span v-if="hoveredEl.clickable" class="et-flag et-clickable">click</span>
            <span v-if="hoveredEl.checked" class="et-flag et-checked">checked</span>
            <span v-if="!hoveredEl.enabled" class="et-flag et-disabled">disabled</span>
          </div>
        </div>
      </div>
      <div class="float-win-meta">
        <span class="float-meta-label">语义</span>
        <span class="float-meta-val">{{ summary }}</span>
      </div>
      <div class="float-win-keys">
        <button class="fkey" @click="sendKey('home')">Home</button>
        <button class="fkey" @click="sendKey('back')">Back</button>
        <button class="fkey" @click="sendKey('recent')">Recent</button>
        <button class="fkey" @click="sendKey('power')">Power</button>
      </div>
      <div class="float-win-resize" @mousedown="onResizeStart"></div>
    </div>
  </transition>
</template>

<script setup>
import { ref, watch, onBeforeUnmount } from 'vue'

const props = defineProps({ online: Boolean, visible: Boolean })
defineEmits(['close'])

const image = ref('')
const summary = ref('')
const elements = ref([])
const showOverlay = ref(true)
const hoveredEl = ref(null)
const tipX = ref(0), tipY = ref(0)
const img = ref(null), cv = ref(null), winRef = ref(null), previewRef = ref(null)
const x = ref(240), y = ref(80), w = ref(320)
let polling = null, dragging = false, dragX = 0, dragY = 0, resizing = false, resizeStart = 0

// ── Position ──
function onDragStart(e) { if (e.button !== 0) return; dragging = true; dragX = e.clientX - x.value; dragY = e.clientY - y.value
  const onMove = ev => { if (!dragging) return; x.value = Math.max(0, Math.min(window.innerWidth - w.value, ev.clientX - dragX)); y.value = Math.max(0, Math.min(window.innerHeight - 100, ev.clientY - dragY)) }
  const onUp = () => { dragging = false; window.removeEventListener('mousemove', onMove); window.removeEventListener('mouseup', onUp) }
  window.addEventListener('mousemove', onMove); window.addEventListener('mouseup', onUp) }
function onResizeStart(e) { e.stopPropagation(); resizing = true; resizeStart = e.clientX - w.value
  const onMove = ev => { if (!resizing) return; w.value = Math.max(220, Math.min(600, ev.clientX - resizeStart)) }
  const onUp = () => { resizing = false; window.removeEventListener('mousemove', onMove); window.removeEventListener('mouseup', onUp) }
  window.addEventListener('mousemove', onMove); window.addEventListener('mouseup', onUp) }

// ── Snapshot ──
async function refresh(force) { if (!props.visible && !force) return
  try { const r = await fetch(`/api/device/snapshot?t=${Date.now()}`, { cache: 'no-store' }); const d = await r.json()
    if (d.status === 'error') return; image.value = (d.screen || {}).image_base64 || ''
    summary.value = (d.understanding || {}).summary || ''; elements.value = (d.understanding || {}).elements || [] } catch(e) {} }
function startPolling() { if (polling) return; polling = setInterval(() => refresh(false), 2500) }
function stopPolling() { if (polling) { clearInterval(polling); polling = null } }
watch(() => props.visible, v => { if (v) { refresh(true); startPolling() } else stopPolling() })
onBeforeUnmount(stopPolling)

// ── Keys ──
async function sendKey(key) { try { await fetch('/api/device/key', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key }) }); setTimeout(() => refresh(true), 350) } catch(e) {} }

// ── Overlay ──
const ROLE_COLORS = { switch: '#409eff', switch_row: '#409eff', navigation_item: '#67c23a', tab: '#e6a23c', list_entry: '#909399', button: '#f56c6c', default: '#909399' }
function drawOverlay() { const im = img.value, c = cv.value; if (!im || !c || !im.complete) return
  const dw = im.naturalWidth || 1080, dh = im.naturalHeight || 1920, sx = im.clientWidth / dw, sy = im.clientHeight / dh
  c.width = im.clientWidth; c.height = im.clientHeight; c.style.cssText = 'position:absolute;top:0;left:0;pointer-events:none'
  const ctx = c.getContext('2d'); ctx.clearRect(0, 0, c.width, c.height)
  if (!showOverlay.value) return
  for (const el of elements.value) { if (!el.bounds || el.bounds.length !== 4) continue
    let [l, t, r, b] = el.bounds; l *= sx; t *= sy; r *= sx; b *= sy; if (r - l < 2 || b - t < 2) continue
    const clr = ROLE_COLORS[el.role] || ROLE_COLORS.default; ctx.strokeStyle = clr; ctx.lineWidth = 1.5
    ctx.strokeRect(l, t, r - l, b - t)
    const lb = (el.label || el.text || el.content_desc || '').substring(0, 12)
    if (lb && (el.clickable || el.role === 'switch')) { ctx.fillStyle = clr; ctx.font = '11px sans-serif'; const tw = ctx.measureText(lb).width + 6
      ctx.fillRect(l, Math.max(0, t - 18), tw, 16); ctx.fillStyle = '#fff'; ctx.fillText(lb, l + 3, Math.max(0, t - 18) + 12) } } }

// ── Tooltip ──
function onMouseMove(e) { const im = img.value; if (!im || !im.complete) { hoveredEl.value = null; return }
  const rect = im.getBoundingClientRect(), mx = e.clientX - rect.left, my = e.clientY - rect.top
  const dw = im.naturalWidth || 1080, dh = im.naturalHeight || 1920, dx = mx * dw / im.clientWidth, dy = my * dh / im.clientHeight
  let found = null
  for (let i = elements.value.length - 1; i >= 0; i--) { const el = elements.value[i]; if (!el.bounds) continue
    const [l, t, r, b] = el.bounds; if (dx >= l && dx <= r && dy >= t && dy <= b) { found = el; break } }
  hoveredEl.value = found
  if (found) { const g = 8; let tx = e.clientX + g, ty = e.clientY - 340 - g; if (ty < 10) ty = e.clientY + g; if (tx + 240 > window.innerWidth - 10) tx = e.clientX - 240 - g; tipX.value = tx; tipY.value = ty } }

defineExpose({ refresh, image })
</script>

<template>
  <!-- 左下角投屏悬浮按钮 -->
  <button class="device-float-toggle"
          :class="{ active: visible, online: deviceOnline }"
          @click="toggle"
          :title="visible ? '隐藏投屏窗口' : '显示投屏窗口'">
    <span class="dft-icon">📱</span>
    <span class="dft-label">{{ visible ? '隐藏投屏' : '设备投屏' }}</span>
    <span class="dft-dot" :class="deviceOnline ? 'dot-online' : 'dot-offline'"></span>
  </button>

  <!-- 设备投屏悬浮窗（全局） -->
  <transition name="floatwin">
    <div v-if="visible" class="device-float-win"
         :style="{ left: posX + 'px', top: posY + 'px', width: winW + 'px' }"
         ref="floatWinRef">
      <div class="float-win-header" @mousedown="onDragStart">
        <span class="float-win-title">📱 设备投屏</span>
        <div class="float-win-actions">
          <el-switch v-model="showOverlay" size="small" active-text="元素" inactive-text=""
                     @change="drawElementOverlay" />
          <button class="float-win-refresh" @click="emit('refresh', true)" :disabled="!deviceOnline"
                  title="手动刷新截图">🔄</button>
          <button class="float-win-close" @click="close">×</button>
        </div>
      </div>
      <div class="preview" ref="previewRef" @mousemove="onPreviewMouseMove" @mouseleave="hoveredElement = null">
        <div v-if="!deviceOnline" class="device-offline-mask">
          <div class="device-offline-content">
            <div class="device-offline-icon">📱</div>
            <div class="device-offline-title">Android 设备未连接</div>
            <div class="device-offline-desc">请检查 USB/ADB 连接</div>
          </div>
        </div>
        <canvas v-if="snapshotImage" ref="previewCanvas" class="preview-canvas" />
        <img v-if="snapshotImage" ref="previewImg" :src="'data:image/png;base64,' + snapshotImage"
             class="preview-img" @load="drawElementOverlay" />
        <!-- 元素属性浮窗（显示全部属性） -->
        <div v-if="hoveredElement" class="element-tooltip"
             :style="{ left: tooltipX + 'px', top: tooltipY + 'px' }">
          <!-- 标题：label（无屏上标签时回退显示经验推断，并标注『经验』） -->
          <div class="et-row et-label">
            {{ hoveredElement.label || (hoveredElement.rag_hint ? '经验: ' + hoveredElement.rag_hint : '(无标签)') }}
          </div>
          <!-- 文本内容 -->
          <div class="et-row" v-if="hoveredElement.text"><span class="et-k">text</span><span class="et-v">{{ hoveredElement.text }}</span></div>
          <div class="et-row" v-if="hoveredElement.content_desc"><span class="et-k">content_desc</span><span class="et-v">{{ hoveredElement.content_desc }}</span></div>
          <div class="et-row" v-if="hoveredElement.associated_label"><span class="et-k">assoc_label</span><span class="et-v">{{ hoveredElement.associated_label }}</span></div>
          <!-- 经验推断语义（来自知识库，非当前界面真实文本） -->
          <div class="et-row" v-if="hoveredElement.rag_hint"><span class="et-k">经验推断</span><span class="et-v et-rag">{{ hoveredElement.rag_hint }}<span class="et-rag-note">（非屏上文本）</span></span></div>
          <!-- 身份标识 -->
          <div class="et-row"><span class="et-k">resource_id</span><span class="et-v">{{ hoveredElement.resource_id || '-' }}</span></div>
          <div class="et-row"><span class="et-k">class_name</span><span class="et-v">{{ hoveredElement.class_name || '-' }}</span></div>
          <div class="et-row" v-if="hoveredElement.package"><span class="et-k">package</span><span class="et-v">{{ hoveredElement.package }}</span></div>
          <!-- 布局 -->
          <div class="et-row"><span class="et-k">bounds</span><span class="et-v">{{ hoveredElement.bounds ? hoveredElement.bounds.join(', ') : '-' }}</span></div>
          <div class="et-row" v-if="hoveredElement.region"><span class="et-k">region</span><span class="et-v">{{ hoveredElement.region }}</span></div>
          <div class="et-row" v-if="hoveredElement.context_path" :title="hoveredElement.context_path"><span class="et-k">path</span><span class="et-v">{{ hoveredElement.context_path }}</span></div>
          <!-- 分类 -->
          <div class="et-row"><span class="et-k">role</span><span class="et-v et-badge" :class="'et-role-' + (hoveredElement.role || 'default')">{{ hoveredElement.role || '-' }}</span></div>
          <div class="et-row" v-if="hoveredElement.priority !== undefined"><span class="et-k">priority</span><span class="et-v">{{ hoveredElement.priority }}</span></div>
          <!-- 状态标记 -->
          <div class="et-row et-flags">
            <span v-if="hoveredElement.clickable" class="et-flag et-clickable">clickable</span>
            <span v-if="!hoveredElement.clickable" class="et-flag et-disabled">not clickable</span>
            <span v-if="hoveredElement.enabled" class="et-flag et-checked">enabled</span>
            <span v-if="!hoveredElement.enabled" class="et-flag et-disabled">disabled</span>
            <span v-if="hoveredElement.selected" class="et-flag et-selected">selected</span>
            <span v-if="hoveredElement.checked === true" class="et-flag et-checked">checked</span>
            <span v-if="hoveredElement.checked === false" class="et-flag et-disabled">unchecked</span>
            <span v-if="hoveredElement.safe_to_click === false" class="et-flag et-disabled">unsafe</span>
            <span v-if="hoveredElement.is_container" class="et-flag et-selected">container</span>
            <span v-if="hoveredElement.has_switch_child" class="et-flag et-clickable">has_switch</span>
          </div>
        </div>
      </div>
      <div class="float-win-meta">
        <span class="float-meta-label">语义</span>
        <span class="float-meta-val">{{ formattedPageSummary }}</span>
      </div>
      <div class="float-win-keys">
        <button class="fkey" @click="emit('send-key', 'home')">Home</button>
        <button class="fkey" @click="emit('send-key', 'back')">Back</button>
        <button class="fkey" @click="emit('send-key', 'recent')">Recent</button>
        <button class="fkey" @click="emit('send-key', 'power')">Power</button>
      </div>
      <div class="float-win-resize" @mousedown="onResizeStart"></div>
    </div>
  </transition>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from "vue";

const props = defineProps({
  deviceOnline: Boolean,
  snapshotImage: String,
  pageSummary: String,
  elementList: Array,
});

const emit = defineEmits(["refresh", "send-key"]);

// ── 窗口状态 ──
const visible = ref(false);
const posX = ref(240);
const posY = ref(80);
const winW = ref(320);
const floatWinRef = ref(null);
let _dragOffX = 0, _dragOffY = 0, _dragging = false;
let _resizing = false, _resizeStartX = 0, _resizeStartW = 0;

// ── 元素覆盖 ──
const showOverlay = ref(true);
const hoveredElement = ref(null);
const tooltipX = ref(0);
const tooltipY = ref(0);
const previewRef = ref(null);
const previewImg = ref(null);
const previewCanvas = ref(null);

const ROLE_COLORS = {
  switch: { stroke: '#6366f1', fill: 'rgba(99,102,241,0.12)' },
  navigation_item: { stroke: '#22c55e', fill: 'rgba(34,197,94,0.10)' },
  tab: { stroke: '#f59e0b', fill: 'rgba(245,158,11,0.10)' },
  list_entry: { stroke: '#9aa0a6', fill: 'rgba(154,160,166,0.08)' },
  settings_entry: { stroke: '#9aa0a6', fill: 'rgba(154,160,166,0.08)' },
  button: { stroke: '#ef4444', fill: 'rgba(239,68,68,0.10)' },
  text: { stroke: '#b0b0b0', fill: 'rgba(176,176,176,0.06)' },
  default: { stroke: '#6366f1', fill: 'rgba(99,102,241,0.08)' },
};

const formattedPageSummary = computed(() => {
  const raw = (props.pageSummary || "").trim();
  if (!raw) return "暂无";
  return raw.replace(/^two_pane/, "双栏").replace(/^single_pane/, "单栏")
            .replace("主要路径入口", "可探索入口");
});

// ── 公开方法 ──
function toggle() {
  visible.value = !visible.value;
  if (visible.value) {
    posX.value = window.innerWidth - winW.value - 24;
    posY.value = 80;
    emit("refresh", true);
  }
}

function close() {
  visible.value = false;
}

// ── 拖拽 ──
function onDragStart(e) {
  if (e.button !== 0) return;
  _dragging = true;
  _dragOffX = e.clientX - posX.value;
  _dragOffY = e.clientY - posY.value;
  const onMove = (ev) => {
    if (!_dragging) return;
    posX.value = Math.max(0, Math.min(window.innerWidth - winW.value, ev.clientX - _dragOffX));
    posY.value = Math.max(0, Math.min(window.innerHeight - 100, ev.clientY - _dragOffY));
  };
  const onUp = () => { _dragging = false; window.removeEventListener('mousemove', onMove); window.removeEventListener('mouseup', onUp); };
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onUp);
  e.preventDefault();
}

// ── 缩放 ──
function onResizeStart(e) {
  if (e.button !== 0) return;
  _resizing = true;
  _resizeStartX = e.clientX;
  _resizeStartW = winW.value;
  const onMove = (ev) => {
    if (!_resizing) return;
    winW.value = Math.max(180, Math.min(window.innerWidth - 40, _resizeStartW + ev.clientX - _resizeStartX));
  };
  const onUp = () => { _resizing = false; window.removeEventListener('mousemove', onMove); window.removeEventListener('mouseup', onUp); };
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onUp);
  e.preventDefault();
}

// ── 元素覆盖绘制 ──
function drawElementOverlay() {
  const img = previewImg.value;
  const canvas = previewCanvas.value;
  if (!img || !canvas || !img.complete) return;

  const dw = img.naturalWidth || 1080;
  const dh = img.naturalHeight || 1920;
  const scaleX = img.clientWidth / dw;
  const scaleY = img.clientHeight / dh;

  canvas.width = img.clientWidth;
  canvas.height = img.clientHeight;
  canvas.style.position = 'absolute';
  canvas.style.top = '0';
  canvas.style.left = '0';
  canvas.style.pointerEvents = 'none';

  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  if (!showOverlay.value) return;

  const elements = props.elementList || [];
  for (const el of elements) {
    if (!el.bounds || el.bounds.length !== 4) continue;
    let [l, t, r, b] = el.bounds;
    l *= scaleX; t *= scaleY; r *= scaleX; b *= scaleY;
    if (r - l < 2 || b - t < 2) continue;

    const role = el.role || 'default';
    const colors = ROLE_COLORS[role] || ROLE_COLORS.default;

    // 经验推断标记：无屏上真实标签、但知识库给出 rag_hint 的元素
    const realLabel = (el.label || el.text || el.content_desc || '').trim();
    const isInferred = !realLabel && !!(el.rag_hint || '').trim();
    // 推断元素用琥珀色明显区分，表明「非当前界面真实所见」
    const boxColors = isInferred
      ? { stroke: '#f59e0b', fill: 'rgba(245,158,11,0.12)' }
      : colors;

    ctx.strokeStyle = boxColors.stroke;
    ctx.fillStyle = boxColors.fill;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.roundRect(l, t, r - l, b - t, 4);
    ctx.fill();
    ctx.stroke();

    // Label（优先真实屏上标签；无则回退到经验推断，并以琥珀色斜体区分）
    const label = (realLabel || el.rag_hint || '').substring(0, 12);
    if (label && (el.clickable || role === 'switch')) {
      ctx.fillStyle = isInferred ? '#f59e0b' : colors.stroke;
      ctx.font = isInferred ? 'italic 11px sans-serif' : '11px sans-serif';
      const textW = ctx.measureText(label).width + 6;
      ctx.fillRect(l, Math.max(0, t - 18), textW, 16);
      ctx.fillStyle = '#fff';
      ctx.fillText(label, l + 3, Math.max(0, t - 18) + 12);
    }
  }
}

// ── 元素悬浮检测 ──
function onPreviewMouseMove(e) {
  const img = previewImg.value;
  if (!img || !img.complete) { hoveredElement.value = null; return; }

  const rect = img.getBoundingClientRect();
  const mx = e.clientX - rect.left;
  const my = e.clientY - rect.top;

  const dw = img.naturalWidth || 1080;
  const dh = img.naturalHeight || 1920;
  const scaleX = dw / img.clientWidth;
  const scaleY = dh / img.clientHeight;
  const dx = mx * scaleX;
  const dy = my * scaleY;

  const elements = props.elementList || [];
  let found = null;
  for (let i = elements.length - 1; i >= 0; i--) {
    const el = elements[i];
    if (!el.bounds || el.bounds.length !== 4) continue;
    const [l, t, r, b] = el.bounds;
    if (dx >= l && dx <= r && dy >= t && dy <= b) {
      found = el;
      break;
    }
  }

  hoveredElement.value = found;

  if (found) {
    const gap = 8;
    const ttW = 240;
    const ttH = 340;
    let tx = e.clientX + gap;
    let ty = e.clientY - ttH - gap;
    if (ty < 10) ty = e.clientY + gap;
    if (tx + ttW > window.innerWidth - 10) tx = e.clientX - ttW - gap;
    tooltipX.value = tx;
    tooltipY.value = ty;
  }
}

// ── 生命周期 ──
onMounted(() => {
  window.addEventListener('resize', drawElementOverlay);
});

onBeforeUnmount(() => {
  window.removeEventListener('resize', drawElementOverlay);
});

defineExpose({ toggle, isVisible: visible });
</script>

<style scoped>
/* ═══════════ Sidebar Device Float Toggle ═══════════ */
.device-float-toggle {
  margin: auto 12px 16px;
  padding: 10px 14px;
  background: var(--bg-sidebar);
  border: 1.5px solid var(--line);
  border-radius: var(--radius-sm);
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text-secondary);
  font-size: 13px;
  font-weight: 500;
  transition: all 0.2s;
  position: relative;
  overflow: hidden;
}
.device-float-toggle::before {
  content: '';
  position: absolute;
  inset: 0;
  background: linear-gradient(135deg, var(--accent-ring), transparent);
  opacity: 0;
  transition: opacity 0.2s;
}
.device-float-toggle:hover { border-color: var(--accent); color: var(--accent); }
.device-float-toggle:hover::before { opacity: 1; }
.device-float-toggle.active { background: var(--accent-light); border-color: var(--accent); color: var(--accent); }
.dft-icon { font-size: 18px; }
.dft-label { flex: 1; text-align: left; }
.dft-dot {
  width: 7px; height: 7px;
  border-radius: 50%;
  flex-shrink: 0;
}
.dot-online  { background: var(--success); box-shadow: 0 0 6px var(--success); }
.dot-offline { background: var(--danger); }

/* ═══════════ Device Float Window ═══════════ */
.device-float-win {
  position: fixed;
  z-index: 2000;
  background: var(--bg-card);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg), 0 0 0 1px var(--line);
  display: flex;
  flex-direction: column;
  min-width: 220px;
  overflow: hidden;
  user-select: none;
}
.float-win-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  background: var(--accent-light);
  cursor: grab;
  border-bottom: 1px solid var(--line);
}
.float-win-header:active { cursor: grabbing; }
.float-win-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--accent);
  letter-spacing: 0.3px;
}
.float-win-actions {
  display: flex;
  align-items: center;
  gap: 2px;
}
.float-win-close {
  margin-left: 8px;
  background: none;
  border: none;
  color: var(--text-muted);
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  padding: 0 4px;
  border-radius: 6px;
  transition: color 0.15s, background 0.15s;
}
.float-win-close:hover { color: var(--danger); background: #fef2f2; }
.float-win-refresh {
  margin-left: 6px;
  background: none;
  border: none;
  color: var(--text-muted);
  font-size: 16px;
  cursor: pointer;
  padding: 2px 6px;
  border-radius: 6px;
  transition: color 0.15s, background 0.15s;
}
.float-win-refresh:hover:not(:disabled) { color: var(--accent); background: var(--accent-light); }
.float-win-refresh:disabled { opacity: 0.3; cursor: not-allowed; }
.float-win-meta {
  display: flex;
  align-items: baseline;
  gap: 6px;
  padding: 5px 10px;
  background: #fafbfc;
  border-top: 1px solid var(--line-light);
}
.float-meta-label {
  font-size: 11px;
  color: var(--text-muted);
  flex-shrink: 0;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.float-meta-val {
  font-size: 11px;
  color: var(--text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.float-win-keys {
  display: flex;
  gap: 4px;
  padding: 6px 8px;
  background: #fafbfc;
  border-top: 1px solid var(--line-light);
}
.fkey {
  flex: 1;
  padding: 5px 0;
  background: #fff;
  border: 1px solid var(--line);
  border-radius: 6px;
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
  transition: all 0.15s;
}
.fkey:hover { background: var(--accent-light); color: var(--accent); border-color: var(--accent); }
.fkey:active { transform: scale(0.95); }
.float-win-resize {
  position: absolute;
  right: 0; top: 0; bottom: 0;
  width: 6px;
  cursor: ew-resize;
  background: transparent;
}
.float-win-resize:hover { background: var(--accent-ring); }

/* ═══════════ Device Preview Overlay ═══════════ */
.preview { position: relative; overflow: hidden; border-radius: var(--radius-sm); background: #f4f5f7; }
.preview-img { width: 100%; display: block; }
.preview-canvas { position: absolute; top: 0; left: 0; width: 100%; height: 100%; }

/* ── Device Offline Mask ── */
.device-offline-mask {
  position: absolute; top: 0; left: 0; right: 0; bottom: 0;
  background: rgba(255, 255, 255, 0.92);
  display: flex; align-items: center; justify-content: center;
  z-index: 10; border-radius: var(--radius-sm);
}
.device-offline-content { text-align: center; color: var(--text-secondary); }
.device-offline-icon { font-size: 48px; margin-bottom: 12px; }
.device-offline-title { font-size: 18px; font-weight: 600; margin-bottom: 6px; color: var(--text-primary); }
.device-offline-desc { font-size: 13px; color: var(--text-muted); }

/* ═══════════ Element Inspector Tooltip ═══════════ */
.element-tooltip {
  position: fixed;
  z-index: 2500;
  min-width: 220px;
  max-width: 360px;
  max-height: 420px;
  overflow-y: auto;
  background: rgba(255, 255, 255, 0.96);
  backdrop-filter: blur(12px);
  border: 1.5px solid var(--accent-ring);
  border-radius: var(--radius-md);
  padding: 10px 12px;
  box-shadow: var(--shadow-lg);
  pointer-events: none;
  font-size: 12px;
  line-height: 1.6;
}
.et-row { display: flex; align-items: baseline; gap: 8px; margin-bottom: 2px; }
.et-label {
  font-size: 14px; font-weight: 700; color: var(--text-primary);
  margin-bottom: 4px; padding-bottom: 4px;
  border-bottom: 1px solid var(--line-light);
}
.et-k {
  min-width: 72px; color: var(--text-muted); font-size: 10px;
  text-transform: uppercase; letter-spacing: 0.3px; flex-shrink: 0;
}
.et-v {
  color: var(--text-secondary); word-break: break-all;
  font-family: 'JetBrains Mono', 'Consolas', monospace;
  font-size: 11px;
}
.et-rag { color: #d97706 !important; font-weight: 600; }
.et-rag-note { color: var(--text-muted); font-size: 10px; font-weight: 400; }
.et-badge {
  display: inline-block;
  padding: 0 6px; border-radius: 4px;
  font-size: 10px; font-weight: 600; letter-spacing: 0.3px;
}
.et-role-switch           { background: var(--accent-light); color: var(--accent); }
.et-role-navigation_item  { background: #f0fdf4; color: #16a34a; }
.et-role-tab              { background: #fffbeb; color: #d97706; }
.et-role-list_entry       { background: #f4f5f7; color: var(--text-secondary); }
.et-role-button           { background: #fef2f2; color: var(--danger); }
.et-role-image            { background: #f4f5f7; color: var(--text-muted); }
.et-role-default          { background: #f7f8fa; color: var(--text-muted); }
.et-flags { margin-top: 4px; gap: 4px; flex-wrap: wrap; }
.et-flag {
  display: inline-block;
  padding: 1px 6px; border-radius: 4px;
  font-size: 10px; font-weight: 600; letter-spacing: 0.3px;
}
.et-clickable { background: var(--accent-light); color: var(--accent); }
.et-checked   { background: #f0fdf4; color: #16a34a; }
.et-selected  { background: #fffbeb; color: #d97706; }
.et-disabled  { background: #fef2f2; color: var(--danger); }

/* ── Float window transition ── */
.floatwin-enter-active { transition: opacity 0.22s ease, transform 0.22s cubic-bezier(.34,1.56,.64,1); }
.floatwin-leave-active  { transition: opacity 0.18s ease, transform 0.18s ease; }
.floatwin-enter-from { opacity: 0; transform: scale(0.88) translateY(-8px); }
.floatwin-leave-to   { opacity: 0; transform: scale(0.92) translateY(-4px); }
</style>

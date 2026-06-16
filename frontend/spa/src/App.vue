<template>
  <el-container class="layout-root">
    <el-aside width="220px" class="side-nav">
      <div class="brand">AI Test Workbench</div>
      <el-menu :default-active="activeMenu" @select="activeMenu = $event">
        <el-menu-item index="workspace">工作台</el-menu-item>
        <el-menu-item index="reports">报告中心</el-menu-item>
        <el-menu-item index="apps">APP 管理</el-menu-item>
        <el-menu-item index="knowledge">知识库</el-menu-item>
      </el-menu>
      <!-- 左下角投屏悬浮按鈕 -->
      <button class="device-float-toggle"
              :class="{ active: deviceWindowVisible, online: deviceOnline }"
              @click="toggleDeviceWindow"
              :title="deviceWindowVisible ? '隐藏投屏窗口' : '显示投屏窗口'">
        <span class="dft-icon">📱</span>
        <span class="dft-label">{{ deviceWindowVisible ? '隐藏投屏' : '设备投屏' }}</span>
        <span class="dft-dot" :class="deviceOnline ? 'dot-online' : 'dot-offline'"></span>
      </button>
    </el-aside>

    <el-container>
      <el-header class="topbar">
        <el-breadcrumb separator="/">
          <el-breadcrumb-item>AI 测试平台</el-breadcrumb-item>
          <el-breadcrumb-item>{{ activeMenu === 'workspace' ? '工作台' : activeMenu === 'reports' ? '报告中心' : activeMenu === 'apps' ? 'APP 管理' : '知识库' }}</el-breadcrumb-item>
        </el-breadcrumb>
        <div class="header-tags">
          <el-tag :type="deviceOnline ? 'success' : 'danger'" size="small">
            {{ deviceOnline ? "设备在线" : "设备离线" }}
          </el-tag>
          <el-tag :type="wsConnected ? 'success' : 'info'" size="small" style="margin-left: 6px">
            {{ wsConnected ? "WS在线" : "HTTP模式" }}
          </el-tag>
        </div>
      </el-header>

      <!-- ═══════════ 工作台 ═══════════ -->
      <el-main class="main-content workspace-main" v-if="activeMenu === 'workspace'">
        <div class="workspace-cols">
          <!-- 左列: 实时日志 + AI 对话 -->
          <div class="workspace-left">
            <!-- 实时日志 -->
            <section class="panel panel-status">
              <div class="panel-header">
                <h3 class="panel-title">实时日志</h3>
                <el-button size="small" @click="logs = []">清空</el-button>
              </div>
              <div class="logs status-logs">
                <div v-for="(log, idx) in logs" :key="idx" class="log-row"
                     :class="{ 'log-warn': log.includes('异常') || log.includes('FAIL'),
                               'log-ok': log.includes('PASS') || log.includes('成功') }">
                  {{ log }}
                </div>
              </div>
            </section>

            <!-- AI 对话与执行 -->
            <section class="panel panel-chat">
              <div class="panel-header">
                <h3 class="panel-title">AI 对话与执行</h3>
                <el-tag v-if="executing" type="warning" size="small">执行中</el-tag>
              </div>
              <div class="chat-list" ref="chatListRef">
                <div v-for="m in messages" :key="m.id" class="bubble" :class="m.type">
                  <div class="bubble-title">{{ m.title }} · {{ m.time }}</div>
                  <div class="bubble-content">{{ m.content }}</div>
                </div>
                <div v-if="streamingToken" class="bubble ai streaming">
                  <div class="bubble-title">AI 思考中 · {{ now() }}</div>
                  <div class="bubble-content">{{ streamingToken }}</div>
                </div>
                <div v-if="currentTool" class="tool-status">
                  <el-icon class="is-loading"><span>⚙</span></el-icon>
                  {{ currentTool }}
                </div>
              </div>
              <el-input v-model="inputText" type="textarea" :rows="3"
                        placeholder="输入测试指令，如: 检查 Settings 的 WLAN 开关是否正常" />
              <div style="margin-top: 8px; text-align: right">
                <el-button type="primary" :loading="executing" @click="startRun">开始执行</el-button>
              </div>
            </section>
          </div>
        </div>
      </el-main>

      <!-- ═══════════ 报告中心 ═══════════ -->
      <el-main class="main-content" v-if="activeMenu === 'reports'">
        <section class="panel">
          <div class="panel-header">
            <h3 class="panel-title">测试报告中心</h3>
            <el-button size="small" @click="loadReports">刷新</el-button>
          </div>
          <el-table :data="reportTasks" border stripe empty-text="暂无报告" @row-click="openReportDetail" row-style="cursor:pointer">
            <el-table-column prop="created_at" label="执行时间" min-width="160">
              <template #default="{ row }">{{ (row.created_at || '').replace('T', ' ').substring(0, 19) }}</template>
            </el-table-column>
            <el-table-column prop="user_request" label="测试用例" min-width="200" show-overflow-tooltip />
            <el-table-column prop="total_steps" label="步骤数" width="80" />
            <el-table-column prop="duration_seconds" label="耗时(s)" width="90" />
            <el-table-column prop="status" label="结果" width="80">
              <template #default="{ row }">
                <el-tag :type="row.status === 'success' ? 'success' : 'danger'" size="small">
                  {{ row.status === 'success' ? '通过' : '失败' }}
                </el-tag>
              </template>
            </el-table-column>
          </el-table>
        </section>
      </el-main>

      <!-- ═══════════ 知识库 ═══════════ -->
      <el-main class="main-content" v-if="activeMenu === 'knowledge'">
        <section class="panel">
          <div class="panel-header">
            <h3 class="panel-title">知识库管理</h3>
            <div style="display:flex;gap:8px;align-items:center">
              <el-tag size="small" type="info">共 {{ kbCount }} 条</el-tag>
              <el-button size="small" @click="loadKbList">刷新</el-button>
              <el-button size="small" type="primary" @click="openKbDialog(null)">+ 新增知识</el-button>
            </div>
          </div>
          <div class="kb-filter-bar">
            <el-input v-model="kbSearchQuery" placeholder="搜索关键词（语义检索）" size="small"
                      style="width:220px" clearable />
            <el-select v-model="kbFilterType" placeholder="知识类型" size="small"
                       style="width:150px" clearable>
              <el-option v-for="t in kbTypes" :key="t.value" :label="t.label" :value="t.value" />
            </el-select>
            <el-input v-model="kbFilterPackage" placeholder="应用包名" size="small"
                      style="width:200px" clearable />
            <el-button size="small" type="primary" @click="searchKb">搜索</el-button>
            <el-button size="small" @click="resetKbFilter">重置</el-button>
            <el-button size="small" type="danger" plain
                       :disabled="!kbFilterPackage && !kbFilterType"
                       @click="deleteKbByFilter">批量删除</el-button>
          </div>
          <el-table :data="kbList" border stripe empty-text="暂无知识数据" style="margin-top:12px">
            <el-table-column type="index" width="50" />
            <el-table-column label="知识类型" width="120">
              <template #default="{ row }">
                <el-tag :type="kbTypeColor(row.metadata && row.metadata.knowledge_type)" size="small">
                  {{ kbTypeLabel(row.metadata && row.metadata.knowledge_type) }}
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
            <el-table-column label="相关度" width="80">
              <template #default="{ row }">
                <span v-if="row.score !== undefined">{{ row.score.toFixed ? row.score.toFixed(2) : row.score }}</span>
                <span v-else>-</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="80" fixed="right">
              <template #default="{ row }">
                <el-button size="small" text type="primary" @click.stop="openKbDetail(row)">详情</el-button>
              </template>
            </el-table-column>
          </el-table>
        </section>
      </el-main>

      <!-- ═══════════ APP 管理 ═══════════ -->
      <el-main class="main-content" v-if="activeMenu === 'apps'">
        <section class="panel">
          <div class="panel-header">
            <h3 class="panel-title">APP 管理</h3>
            <div style="display:flex;gap:8px">
              <el-button size="small" @click="loadApps">刷新</el-button>
              <el-button size="small" type="primary" @click="openAppDialog(null)">+ 添加</el-button>
            </div>
          </div>
          <el-table :data="appList" border stripe empty-text="暂无应用，点击右上角添加">
            <el-table-column prop="name" label="应用名称" min-width="120" />
            <el-table-column prop="package" label="包名" min-width="220" show-overflow-tooltip />
            <el-table-column label="触发关键词" min-width="200">
              <template #default="{ row }">
                <el-tag v-for="kw in (row.keywords || [])" :key="kw"
                        size="small" style="margin:2px">{{ kw }}</el-tag>
                <span v-if="!(row.keywords && row.keywords.length)" style="color:#999;font-size:12px">未设置</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="130" fixed="right">
              <template #default="{ row }">
                <el-button size="small" text type="primary" @click.stop="openAppDialog(row)">编辑</el-button>
                <el-button size="small" text type="danger" @click.stop="deleteApp(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
        </section>
      </el-main>
  
    </el-container>
  </el-container>
  
  <!-- ═══════════ 设备投屏悬浮窗（全局） ═══════════ -->
  <transition name="floatwin">
    <div v-if="deviceWindowVisible" class="device-float-win"
         :style="{ left: floatWinX + 'px', top: floatWinY + 'px', width: floatWinW + 'px' }"
         ref="floatWinRef">
      <div class="float-win-header" @mousedown="onFloatWinDragStart">
        <span class="float-win-title">📱 设备投屏</span>
        <div class="float-win-actions">
          <el-switch v-model="showElementOverlay" size="small" active-text="元素" inactive-text=""
                     @change="drawElementOverlay" />
          <el-button size="small" text @click="refreshSnapshot(true)" :disabled="!deviceOnline"
                     style="margin-left:6px;color:#fff;">刷新</el-button>
          <button class="float-win-close" @click="deviceWindowVisible = false">×</button>
        </div>
      </div>
      <div class="preview" ref="previewRef">
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
      </div>
      <div class="float-win-meta">
        <span class="float-meta-label">语义</span>
        <span class="float-meta-val">{{ formattedPageSummary }}</span>
      </div>
      <div class="float-win-keys">
        <button class="fkey" @click="sendDeviceKey('home')">Home</button>
        <button class="fkey" @click="sendDeviceKey('back')">Back</button>
        <button class="fkey" @click="sendDeviceKey('recent')">Recent</button>
        <button class="fkey" @click="sendDeviceKey('power')">Power</button>
      </div>
      <div class="float-win-resize" @mousedown="onFloatWinResizeStart"></div>
    </div>
  </transition>
  
  <!-- ═══════════ 测试目标确认对话框（可编辑）═══════════ -->
  <el-dialog v-model="planReviewVisible" width="560px" :close-on-click-modal="false"
             :close-on-press-escape="false" class="plan-review-dialog">
    <template #header>
      <div class="pr-title">
        <span class="pr-title-icon">🎯</span>
        <span>测试目标确认（可编辑）</span>
      </div>
    </template>

    <div class="pr-body">
      <div class="pr-section">
        <div class="pr-section-label">目标</div>
        <el-input v-model="planReviewGoal" type="textarea" :rows="2" placeholder="测试目标" />
      </div>
      <div class="pr-section">
        <div class="pr-section-label">目标页面</div>
        <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:6px">
          <el-tag v-for="(p, i) in planReviewPages" :key="i" size="small" closable @close="planReviewPages.splice(i,1)">{{ p }}</el-tag>
        </div>
        <div style="display:flex;gap:6px">
          <el-input v-model="newPageName" size="small" placeholder="添加页面" @keyup.enter="addReviewPage" style="flex:1" />
          <el-button size="small" @click="addReviewPage">+</el-button>
        </div>
      </div>
      <div class="pr-section">
        <div class="pr-section-label">验证条件</div>
        <div v-for="(v, i) in planReviewVerifications" :key="i" style="display:flex;gap:6px;margin-bottom:4px">
          <el-input v-model="planReviewVerifications[i]" size="small" />
          <el-button size="small" type="danger" text @click="planReviewVerifications.splice(i,1)">×</el-button>
        </div>
        <el-button size="small" @click="planReviewVerifications.push('')">+ 添加验证</el-button>
      </div>
      <div class="pr-section">
        <div class="pr-section-label">导航提示</div>
        <div v-for="(h, i) in planReviewHints" :key="i" style="display:flex;gap:6px;margin-bottom:4px">
          <el-input v-model="planReviewHints[i]" size="small" />
          <el-button size="small" type="danger" text @click="planReviewHints.splice(i,1)">×</el-button>
        </div>
        <el-button size="small" @click="planReviewHints.push('')">+ 添加提示</el-button>
      </div>
    </div>

    <template #footer>
      <div class="pr-footer">
        <el-button size="large" @click="confirmPlan('cancel')">取消</el-button>
        <el-button size="large" type="primary" @click="confirmPlan('confirm')">
          确认并开始执行
        </el-button>
      </div>
    </template>
  </el-dialog>

  <!-- ═══════════ 元素身份确认对话框 ═══════════ -->
  <el-dialog v-model="identityDialogVisible" title="确认元素映射" width="520px"
             :close-on-click-modal="false" :close-on-press-escape="false">
    <div class="identity-dialog-msg">
      以下元素存在多个匹配项，LLM 推理选择了其中一个。确认写入知识库？
    </div>
    <div class="identity-list">
      <div v-for="(item, idx) in identityPending" :key="idx" class="identity-row">
        <el-checkbox v-model="item._confirmed" :label="item.target" size="small">
          <span class="id-alias">{{ item.target }}</span>
        </el-checkbox>
        <span class="id-detail">
          → rid={{ item.resource_id }} class={{ item.class_name }} role={{ item.role }}
          ({{ item.candidates_count }} 候选)
        </span>
      </div>
    </div>
    <template #footer>
      <el-button @click="identityDialogVisible = false">跳过</el-button>
      <el-button type="primary" @click="confirmIdentities">确认选中项</el-button>
    </template>
  </el-dialog>

  <!-- ═══════════ 人工确认对话框 ═══════════ -->
  <el-dialog v-model="humanDialogVisible" title="需要人工确认" width="420px"
             :close-on-click-modal="false" :close-on-press-escape="false">
    <div class="human-question">{{ humanQuestion }}</div>
    <div style="margin-top: 12px; color: var(--muted); font-size: 13px;">
      步骤: {{ humanStep }} | 操作: {{ humanAction }}
    </div>
    <template #footer>
      <el-button @click="sendHumanDecision('跳过此步')" :disabled="humanDeciding">跳过此步</el-button>
      <el-button type="danger" @click="sendHumanDecision('终止测试')" :disabled="humanDeciding">终止测试</el-button>
      <el-button type="primary" @click="sendHumanDecision('允许执行')" :disabled="humanDeciding" :loading="humanDeciding">
        允许执行
      </el-button>
    </template>
  </el-dialog>

  <!-- ═══════════ 用例编辑器 ═══════════ -->
  <el-dialog v-model="caseEditorVisible" title="测试用例 YAML" width="62%">
    <div style="margin-bottom: 8px; color: var(--muted); font-size: 12px;">
      {{ caseEditorFile }}
    </div>
    <el-input v-model="caseEditorContent" type="textarea" :rows="22"
              :disabled="loadingCaseEditor || savingCaseEditor" />
    <template #footer>
      <el-button @click="caseEditorVisible = false">关闭</el-button>
      <el-button :loading="savingCaseEditor" @click="saveCaseContent(false)">保存</el-button>
      <el-button type="primary" :loading="savingCaseEditor" @click="saveCaseContent(true)">
        保存并执行
      </el-button>
    </template>
  </el-dialog>

  <!-- ═══════════ 报告详情 ═══════════ -->
  <el-dialog v-model="reportDetailVisible" title="测试报告详情" width="68%" top="3vh">
    <div v-if="selectedReport" class="report-detail">
      <!-- 头部摘要 -->
      <div class="report-header">
        <div class="report-title">{{ selectedReport.user_request || '测试报告' }}</div>
        <div class="report-meta">
          <el-tag :type="selectedReport.status === 'success' ? 'success' : 'danger'" size="default">
            {{ selectedReport.status === 'success' ? '✅ 通过' : '❌ 失败' }}
          </el-tag>
          <span>总耗时 {{ selectedReport.duration_seconds || 0 }}s</span>
          <span>{{ (selectedReport.created_at || '').replace('T', ' ').substring(0, 19) }}</span>
        </div>
        <div class="report-summary">
          <div class="stat"><b>{{ selectedReport.total_steps }}</b> 总步骤</div>
          <div class="stat pass"><b>{{ selectedReport.pass_count }}</b> 通过</div>
          <div class="stat fail"><b>{{ selectedReport.fail_count }}</b> 失败</div>
          <div class="stat"><b>{{ selectedReport.total_steps ? Math.round(selectedReport.pass_count / selectedReport.total_steps * 100) : 0 }}%</b> 通过率</div>
        </div>
      </div>
      <!-- 步骤时间线 -->
      <div class="report-steps">
        <div class="step-card" v-for="s in (selectedReport.steps || [])" :key="s.index"
             :class="'step-' + (s.status || 'unknown')">
          <div class="step-icon">
            <span v-if="s.status === 'success'">✅</span>
            <span v-else-if="s.status === 'fail'">❌</span>
            <span v-else-if="s.status === 'continue'">🏃</span>
            <span v-else>⚠️</span>
          </div>
          <div class="step-body">
            <div class="step-title">
              <b>步骤 {{ s.index }}:</b> {{ s.intent || '—' }}
              <span class="step-time">{{ s.started_at ? s.started_at.replace('T', ' ').substring(10, 19) : '' }}</span>
            </div>
            <div class="step-action">操作: <code>{{ s.action_type || '—' }}</code> → {{ s.target || '—' }}</div>
            <div v-if="s.expected" class="step-expected">预期: {{ s.expected }}</div>
            <div class="step-obs" :class="'obs-' + (s.status || '')">
              <div class="obs-summary">{{ s.observation || '无观察结果' }}</div>
              <div v-if="s.raw_observation && s.raw_observation !== s.observation" style="margin-top: 4px;">
                <el-button size="small" text type="primary" @click="toggleStepDetail(s.index)">
                  {{ expandedSteps.has(s.index) ? '收起细节 ▲' : '展开细节 ▼' }}
                </el-button>
                <div v-if="expandedSteps.has(s.index)" class="obs-detail">
                  <pre>{{ s.raw_observation }}</pre>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
      <!-- 结论 -->
      <div class="report-conclusion" v-if="selectedReport.conclusion">
        <h4>📝 测试结论</h4>
        <div style="white-space: pre-wrap;">{{ selectedReport.conclusion }}</div>
      </div>
    </div>
  </el-dialog>

  <!-- ═══════════ 知识库新增对话框 ═══════════ -->
  <el-dialog v-model="kbDialogVisible" title="新增知识" width="560px" :close-on-click-modal="false">
    <el-form :model="kbForm" label-width="90px" style="padding-right:12px">
      <el-form-item label="应用包名" required>
        <el-input v-model="kbForm.app_package" placeholder="如: com.android.settings" />
      </el-form-item>
      <el-form-item label="知识类型" required>
        <el-select v-model="kbForm.knowledge_type" style="width:100%" placeholder="请选择">
          <el-option v-for="t in kbTypes" :key="t.value" :label="t.label" :value="t.value" />
        </el-select>
      </el-form-item>
      <el-form-item label="内容" required>
        <el-input v-model="kbForm.content" type="textarea" :rows="5"
                  placeholder="知识内容，支持语义检索" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="kbDialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="kbSaving" @click="saveKb">保存</el-button>
    </template>
  </el-dialog>

  <!-- ═══════════ 知识库详情对话框 ═══════════ -->
  <el-dialog v-model="kbDetailVisible" title="知识详情" width="600px">
    <div v-if="kbDetailRow" class="kb-detail">
      <div class="kb-detail-row">
        <span class="kb-detail-label">知识类型</span>
        <el-tag :type="kbTypeColor(kbDetailRow.metadata && kbDetailRow.metadata.knowledge_type)">
          {{ kbTypeLabel(kbDetailRow.metadata && kbDetailRow.metadata.knowledge_type) }}
        </el-tag>
      </div>
      <div class="kb-detail-row">
        <span class="kb-detail-label">应用包名</span>
        <span>{{ (kbDetailRow.metadata && kbDetailRow.metadata.app_package) || '-' }}</span>
      </div>
      <div class="kb-detail-row">
        <span class="kb-detail-label">时间</span>
        <span>{{ ((kbDetailRow.metadata && kbDetailRow.metadata.timestamp) || '').replace('T', ' ') }}</span>
      </div>
      <div class="kb-detail-row">
        <span class="kb-detail-label">相关度</span>
        <span>{{ kbDetailRow.score !== undefined ? (kbDetailRow.score.toFixed ? kbDetailRow.score.toFixed(4) : kbDetailRow.score) : '-' }}</span>
      </div>
      <div class="kb-detail-content">
        <div class="kb-detail-label" style="margin-bottom:8px">内容</div>
        <div class="kb-detail-text">{{ kbDetailRow.content }}</div>
      </div>
      <div v-if="kbDetailRow.metadata" class="kb-detail-content">
        <div class="kb-detail-label" style="margin-bottom:8px">元数据</div>
        <pre class="kb-detail-meta">{{ JSON.stringify(kbDetailRow.metadata, null, 2) }}</pre>
      </div>
    </div>
    <template #footer>
      <el-button type="danger" plain @click="deleteKbItem(kbDetailRow)">删除此条</el-button>
      <el-button @click="kbDetailVisible = false">关闭</el-button>
    </template>
  </el-dialog>

  <!-- ═══════════ APP 增改对话框 ═══════════ -->
  <el-dialog v-model="appDialogVisible"
             :title="appDialogMode === 'add' ? '添加应用' : '编辑应用'"
             width="520px" :close-on-click-modal="false">
    <el-form :model="appForm" label-width="90px" style="padding-right:12px">
      <el-form-item label="应用名称" required>
        <el-input v-model="appForm.name" placeholder="如: Settings、设置" />
      </el-form-item>
      <el-form-item label="包名" required>
        <el-input v-model="appForm.package"
                  placeholder="如: com.android.settings"
                  :disabled="appDialogMode === 'edit'" />
        <div v-if="appDialogMode === 'edit'" style="font-size:12px;color:#999;margin-top:4px">包名不可修改</div>
      </el-form-item>
      <el-form-item label="触发关键词">
        <div class="kw-editor">
          <div class="kw-list">
            <el-tag v-for="(kw, idx) in appForm.keywords" :key="idx"
                    closable @close="removeKeyword(idx)"
                    style="margin:3px">{{ kw }}</el-tag>
          </div>
          <div class="kw-input-row">
            <el-input v-model="newKeyword" placeholder="输入关键词后按 Enter 添加"
                      size="small" @keyup.enter="addKeyword" style="flex:1" />
            <el-button size="small" @click="addKeyword">添加</el-button>
          </div>
        </div>
        <div style="font-size:12px;color:#999;margin-top:4px">
          用户输入测试指令时，包含任意关键词即自动匹配该应用
        </div>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="appDialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="appSaving" @click="saveApp">保存</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

const activeMenu = ref("workspace");
const inputText = ref("");
const executing = ref(false);
const wsConnected = ref(false);
const deviceOnline = ref(false);
const devicePolling = ref(false);
const snapshotImage = ref("");
const pageSummary = ref("");
const primaryPaths = ref([]);
const elementList = ref([]);
const showElementOverlay = ref(true);
const previewRef = ref(null);
const previewImg = ref(null);
const previewCanvas = ref(null);
const logs = ref([]);
const messages = ref([]);
const streamingToken = ref("");
const currentTool = ref("");
const chatListRef = ref(null);

// 计划审阅
const planReviewVisible = ref(false);
const planReviewGoal = ref("");
const planReviewPages = ref([]);
const planReviewVerifications = ref([]);
const planReviewHints = ref([]);
const planReviewSubmitting = ref(false);
const newPageName = ref("");

function addReviewPage() {
  const name = newPageName.value.trim();
  if (name && !planReviewPages.value.includes(name)) {
    planReviewPages.value.push(name);
  }
  newPageName.value = "";
}

// 元素身份确认
const identityDialogVisible = ref(false);
const identityPending = ref([]);

// 人工确认
const humanDialogVisible = ref(false);
const humanQuestion = ref("");
const humanStep = ref(0);
const humanAction = ref("");
const humanDeciding = ref(false);
const currentThreadId = ref("");

// 用例编辑器
const caseEditorVisible = ref(false);
const caseEditorContent = ref("");
const caseEditorFile = ref("");
const loadingCaseEditor = ref(false);
const savingCaseEditor = ref(false);

// 报告
const reportTasks = ref([]);
const reportDetailVisible = ref(false);
const selectedReport = ref(null);
const expandedSteps = ref(new Set());

// APP 管理
const appList = ref([]);
const appDialogVisible = ref(false);
const appDialogMode = ref('add');
const appSaving = ref(false);
const newKeyword = ref('');
const appForm = ref({ name: '', package: '', keywords: [] });

// 设备投屏悬浮窗
const deviceWindowVisible = ref(false);
const floatWinX = ref(240);
const floatWinY = ref(80);
const floatWinW = ref(320);
const floatWinRef = ref(null);
let _dragOffX = 0, _dragOffY = 0, _dragging = false;
let _resizing = false, _resizeStartX = 0, _resizeStartW = 0;

function toggleDeviceWindow() {
  deviceWindowVisible.value = !deviceWindowVisible.value;
  if (deviceWindowVisible.value) {
    floatWinX.value = window.innerWidth - floatWinW.value - 24;
    floatWinY.value = 80;
    refreshSnapshot(true);
  } else {
    stopSnapshotPolling();  // 关闭投屏时停止定时获取截图
  }
}

function onFloatWinDragStart(e) {
  if (e.button !== 0) return;
  _dragging = true;
  _dragOffX = e.clientX - floatWinX.value;
  _dragOffY = e.clientY - floatWinY.value;
  const onMove = (ev) => {
    if (!_dragging) return;
    floatWinX.value = Math.max(0, Math.min(window.innerWidth - floatWinW.value, ev.clientX - _dragOffX));
    floatWinY.value = Math.max(0, Math.min(window.innerHeight - 100, ev.clientY - _dragOffY));
  };
  const onUp = () => { _dragging = false; window.removeEventListener('mousemove', onMove); window.removeEventListener('mouseup', onUp); };
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onUp);
  e.preventDefault();
}

function onFloatWinResizeStart(e) {
  if (e.button !== 0) return;
  _resizing = true;
  _resizeStartX = e.clientX;
  _resizeStartW = floatWinW.value;
  const onMove = (ev) => {
    if (!_resizing) return;
    floatWinW.value = Math.max(220, Math.min(600, _resizeStartW + ev.clientX - _resizeStartX));
  };
  const onUp = () => { _resizing = false; window.removeEventListener('mousemove', onMove); window.removeEventListener('mouseup', onUp); };
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onUp);
  e.preventDefault();
}

// 知识库
const kbList = ref([]);
const kbCount = ref(0);
const kbSearchQuery = ref('');
const kbFilterType = ref('');
const kbFilterPackage = ref('');
const kbDialogVisible = ref(false);
const kbDetailVisible = ref(false);
const kbDetailRow = ref(null);
const kbSaving = ref(false);
const kbForm = ref({ app_package: '', knowledge_type: 'test_experience', content: '' });
const kbTypes = [
  { value: 'page_structure', label: '页面结构' },
  { value: 'navigation_path', label: '导航路径' },
  { value: 'test_experience', label: '测试经验' },
  { value: 'element_identity', label: '元素身份' },
  { value: 'verified_plan', label: '验证计划' },
];
const kbTypeColorMap = {
  page_structure: 'info',
  navigation_path: 'success',
  test_experience: 'warning',
  element_identity: 'primary',
  verified_plan: 'danger',
};
function kbTypeLabel(type) {
  const t = kbTypes.find(x => x.value === type);
  return t ? t.label : (type || '未知');
}
function kbTypeColor(type) {
  return kbTypeColorMap[type] || 'info';
}

function toggleStepDetail(index) {
  const s = new Set(expandedSteps.value);
  if (s.has(index)) s.delete(index); else s.add(index);
  expandedSteps.value = s;
}

let ws = null;
let snapshotTimer = null;
let snapshotInFlight = false;
let streamTokenTimer = null;
let deviceStatusTimer = null;

function now() { return new Date().toLocaleTimeString(); }

const formattedPageSummary = computed(() => {
  const raw = (pageSummary.value || "").trim();
  if (!raw) return "暂无";
  return raw.replace(/^two_pane/, "双栏").replace(/^single_pane/, "单栏")
            .replace("主要路径入口", "可探索入口");
});

const formattedPrimaryPaths = computed(() => {
  const labels = [];
  const seen = new Set();
  for (const item of primaryPaths.value || []) {
    const raw = String(item?.label || item?.text || item?.content_desc || item?.resource_id || "").trim();
    if (!raw || raw.includes(":id/")) continue;
    if (/^[a-zA-Z0-9_.:-]+$/.test(raw) && !/wifi|bluetooth|setting|search/i.test(raw)) continue;
    if (/^collapse$/i.test(raw)) continue;
    const n = raw.replace(/\s+/g, " ").trim();
    if (seen.has(n)) continue;
    seen.add(n);
    labels.push(n);
    if (labels.length >= 8) break;
  }
  return labels.length ? labels.join(" / ") : "暂无";
});

function addLog(text) {
  logs.value.push(`${now()} ${text}`);
  if (logs.value.length > 500) logs.value.shift();
}

function addMessage(type, title, content) {
  messages.value.push({
    id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
    type, title,
    content: String(content || ""),
    time: now(),
  });
}

// ═══════════ WebSocket 事件处理 ═══════════

function handleEvent(data) {
  const type = data.type;
  const content = data.content || {};

  switch (type) {
    case "status":
      addLog(`状态: ${content}`);
      addMessage("ai", "AI状态", content);
      refreshSnapshot();
      break;

    case "plan_review":
      // 显示目标确认对话框
      flushStreamToken();
      const pd = content.plan || content;
      planReviewGoal.value = pd.goal || content.goal || "";
      planReviewPages.value = pd.target_pages || content.pages || [];
      planReviewVerifications.value = pd.verification || content.verification || [];
      planReviewHints.value = pd.hints || [];
      planReviewVisible.value = true;
      addLog(`测试目标: ${planReviewGoal.value}`);
      break;

    case "plan_ready":
      addLog(`测试目标已生成: ${content.goal || content.steps || "?"}`);
      break;

    case "stream_token":
      // 流式 token：累积到 streamingToken，定时刷新
      streamingToken.value += String(content || "");
      resetStreamTimer();
      break;

    case "tool_start":
      currentTool.value = `正在执行: ${content.name || ""}`;
      addLog(`🔧 ${content.name || "tool"}`);
      break;

    case "tool_end":
      currentTool.value = "";
      if (content.name) addLog(`   ✓ ${content.name} 完成`);
      break;

    case "step_start":
      addLog(`▶ 步骤开始: ${content.content || content.step || ""}`);
      refreshSnapshot();
      break;

    case "step_end":
      addLog(`   ✓ 步骤结束: ${content.content || content.status || ""}`);
      refreshSnapshot();
      break;

    case "snapshot":
      if (content.image) snapshotImage.value = content.image;
      break;

    case "anomaly":
      addMessage("error", "异常告警", content.message || JSON.stringify(content));
      addLog(`⚠ 异常: ${content.message || content.description || ""}`);
      break;

    case "need_human_approval":
      // 人工确认
      currentThreadId.value = content.thread_id || currentThreadId.value;
      humanQuestion.value = content.question || "是否继续执行?";
      humanStep.value = content.step || 0;
      humanAction.value = content.action || "";
      humanDialogVisible.value = true;
      executing.value = false;
      addLog(`⏸ 需要人工确认: ${humanQuestion.value}`);
      break;

    case "result":
      // need_human → 触发计划审阅或人工确认
      if (content.status === "need_human" || content.interrupt) {
        const intr = content.interrupt || content;
        if (intr.type === "plan_review") {
          const planData = intr.plan || {};
          planReviewGoal.value = planData.goal || intr.goal || "";
          planReviewPages.value = planData.target_pages || intr.pages || [];
          planReviewVerifications.value = planData.verification || intr.verification || [];
          planReviewHints.value = planData.hints || [];
          planReviewVisible.value = true;
          currentThreadId.value = content.thread_id || "";
          addLog(`测试目标: ${planReviewGoal.value}`);
        } else {
          // 人工确认
          humanQuestion.value = intr.question || "是否继续?";
          humanStep.value = intr.step || 0;
          humanAction.value = intr.action || "";
          humanDialogVisible.value = true;
          addLog("需要人工确认");
        }
        executing.value = false;
        stopSnapshotPolling();
        break;
      }
      // Level2 元素身份确认
      const pendingIds = content.pending_identities || [];
      if (content.status === "success" && pendingIds.length > 0) {
        const level2 = pendingIds.filter(p => p.level === 2);
        if (level2.length > 0) {
          identityPending.value = level2;
          identityDialogVisible.value = true;
          currentThreadId.value = content.thread_id || "";
          addLog(`发现 ${level2.length} 个待确认的元素映射`);
        }
      }
      // 最终结果
      executing.value = false;
      stopSnapshotPolling();
      flushStreamToken();
      currentTool.value = "";
      addMessage("ai", "执行结果", content.conclusion || content.message || JSON.stringify(content));
      addLog(`${content.status === "success" ? "PASS" : "FAIL"}: ${(content.conclusion || "").substring(0, 200)}`);
      refreshSnapshot();
      loadReports();
      break;

    case "error":
      addLog(`❌ 错误: ${content}`);
      executing.value = false;
      stopSnapshotPolling();
      break;

    default:
      addLog(`[${type}] ${JSON.stringify(content).substring(0, 200)}`);
  }
}

function resetStreamTimer() {
  if (streamTokenTimer) clearTimeout(streamTokenTimer);
  streamTokenTimer = setTimeout(flushStreamToken, 2000);
}

function flushStreamToken() {
  if (streamingToken.value.trim()) {
    addMessage("ai", "AI输出", streamingToken.value.trim());
    streamingToken.value = "";
  }
  if (streamTokenTimer) {
    clearTimeout(streamTokenTimer);
    streamTokenTimer = null;
  }
}

// ═══════════ WebSocket 连接 ═══════════

function connectWS() {
  ws = new WebSocket(`ws://${location.host}/ws/chat`);
  ws.onopen = () => {
    wsConnected.value = true;
    addLog("WebSocket 已连接");
  };
  ws.onclose = () => {
    wsConnected.value = false;
    setTimeout(connectWS, 3000);
  };
  ws.onmessage = (e) => handleEvent(JSON.parse(e.data));
}

// ═══════════ 一键执行 ═══════════

async function startRun() {
  const text = inputText.value.trim();
  if (!text) return;
  if (!deviceOnline.value) {
    ElMessage.warning("Android 设备未连接，请先连接设备");
    return;
  }
  addMessage("user", "用户指令", text);
  inputText.value = "";
  executing.value = true;
  streamingToken.value = "";
  currentTool.value = "";
  startSnapshotPolling();

  if (wsConnected.value && ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "run", message: text }));
    return;
  }
  // HTTP fallback
  try {
    const res = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
    });
    const data = await res.json();
    handleEvent({ type: "result", content: data.data || data });
  } catch (e) {
    handleEvent({ type: "error", content: String(e) });
  }
}

// ═══════════ 人工确认 ═══════════

async function sendHumanDecision(decision) {
  humanDeciding.value = true;
  executing.value = true;
  humanDialogVisible.value = false;
  addLog(`人工决定: ${decision}`);
  startSnapshotPolling();

  if (wsConnected.value && ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "human_decision", thread_id: currentThreadId.value, decision }));
    humanDeciding.value = false;
    return;
  }
  // HTTP fallback
  try {
    const res = await fetch("/api/human_decision", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ thread_id: currentThreadId.value, decision }),
    });
    const data = await res.json();
    handleEvent({ type: "result", content: data.data || data });
  } catch (e) {
    handleEvent({ type: "error", content: String(e) });
  } finally {
    humanDeciding.value = false;
  }
}

// ═══════════ 元素身份确认 ═══════════

async function confirmIdentities() {
  const confirmed = identityPending.value.filter(p => p._confirmed);
  if (confirmed.length === 0) {
    identityDialogVisible.value = false;
    return;
  }
  try {
    await fetch("/api/element_identities/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ identities: confirmed }),
    });
    addLog(`已确认 ${confirmed.length} 个元素映射`);
  } catch (e) { /* ignore */ }
  identityDialogVisible.value = false;
}

// ═══════════ 计划审阅 ═══════════

async function confirmPlan(action) {
  if (planReviewSubmitting.value) return;  // 防抖：已在提交中
  planReviewSubmitting.value = true;
  planReviewVisible.value = false;
  executing.value = true;
  startSnapshotPolling();

  const resumePayload = action === "cancel" ? "cancel" : {
    action: "confirm",
    goal: planReviewGoal.value,
    target_pages: planReviewPages.value.filter(p => p.trim()),
    verification: planReviewVerifications.value.filter(v => v.trim()),
    hints: planReviewHints.value.filter(h => h.trim()),
  };

  addLog(action === "cancel" ? "目标已取消" : `目标已确认: ${planReviewGoal.value}`);

  if (wsConnected.value && ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "human_decision", thread_id: currentThreadId.value, decision: resumePayload }));
    planReviewSubmitting.value = false;
    return;
  }

  try {
    const res = await fetch("/api/human_decision", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ thread_id: currentThreadId.value, decision: resumePayload }),
    });
    const data = await res.json();
    handleEvent({ type: "result", content: data.data || data });
  } catch (e) {
    handleEvent({ type: "error", content: String(e) });
  } finally {
    planReviewSubmitting.value = false;
  }
}

// ═══════════ 元素覆盖绘制 ═══════════

const ROLE_COLORS = {
  switch: { stroke: '#409eff', fill: 'rgba(64,158,255,0.12)' },
  navigation_item: { stroke: '#67c23a', fill: 'rgba(103,194,58,0.10)' },
  tab: { stroke: '#e6a23c', fill: 'rgba(230,162,60,0.10)' },
  list_entry: { stroke: '#909399', fill: 'rgba(144,147,153,0.08)' },
  settings_entry: { stroke: '#909399', fill: 'rgba(144,147,153,0.08)' },
  button: { stroke: '#f56c6c', fill: 'rgba(245,108,108,0.10)' },
  text: { stroke: '#b0b0b0', fill: 'rgba(176,176,176,0.06)' },
  default: { stroke: '#409eff', fill: 'rgba(64,158,255,0.08)' },
};

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

  if (!showElementOverlay.value) return;

  const elements = elementList.value || [];
  for (const el of elements) {
    if (!el.bounds || el.bounds.length !== 4) continue;
    let [l, t, r, b] = el.bounds;
    l *= scaleX; t *= scaleY; r *= scaleX; b *= scaleY;
    if (r - l < 2 || b - t < 2) continue;

    const role = el.role || 'default';
    const colors = ROLE_COLORS[role] || ROLE_COLORS.default;

    ctx.strokeStyle = colors.stroke;
    ctx.fillStyle = colors.fill;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.roundRect(l, t, r - l, b - t, 4);
    ctx.fill();
    ctx.stroke();

    // Label
    const label = (el.label || el.text || el.content_desc || '').substring(0, 12);
    if (label && (el.clickable || role === 'switch')) {
      ctx.fillStyle = colors.stroke;
      ctx.font = '11px sans-serif';
      const textW = ctx.measureText(label).width + 6;
      ctx.fillRect(l, Math.max(0, t - 18), textW, 16);
      ctx.fillStyle = '#fff';
      ctx.fillText(label, l + 3, Math.max(0, t - 18) + 12);
    }
  }
}

// ═══════════ 设备操作 ═══════════

async function refreshSnapshot(force = false) {
  if (!deviceWindowVisible.value && !force) return;  // 投屏窗口未打开时跳过
  if (snapshotInFlight && !force) return;
  snapshotInFlight = true;
  try {
    const res = await fetch(`/api/device/snapshot?t=${Date.now()}`, { cache: "no-store" });
    if (res.status === 503) {
      deviceOnline.value = false;
      startDevicePolling();
      return;
    }
    const data = await res.json();
    if (data.status === "error") {
      deviceOnline.value = false;
      startDevicePolling();
      return;
    }
    deviceOnline.value = true;
    stopDevicePolling();
    snapshotImage.value = (data.screen || {}).image_base64 || "";
    pageSummary.value = (data.understanding || {}).summary || "";
    primaryPaths.value = (data.understanding || {}).primary_paths || [];
    elementList.value = (data.understanding || {}).elements || [];
    setTimeout(drawElementOverlay, 100);
  } catch (e) {
    deviceOnline.value = false;
    startDevicePolling();
  } finally {
    snapshotInFlight = false;
  }
}

async function sendDeviceKey(key) {
  try {
    await fetch("/api/device/key", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key }),
    });
    addLog(`设备按键: ${key}`);
    setTimeout(() => refreshSnapshot(true), 350);
  } catch (e) {
    ElMessage.error(`按键失败: ${e}`);
  }
}

// ═══════════ 用例编辑 ═══════════

async function openCaseEditor(caseFile) {
  if (!caseFile) return;
  loadingCaseEditor.value = true;
  try {
    const res = await fetch(`/api/cases/content?case_file=${encodeURIComponent(caseFile)}`, { cache: "no-store" });
    const data = await res.json();
    if (data.status !== "success") { ElMessage.error(data.message || "读取失败"); return; }
    caseEditorFile.value = data.case_file || caseFile;
    caseEditorContent.value = data.content || "";
    caseEditorVisible.value = true;
  } catch (e) {
    ElMessage.error(`读取失败: ${e}`);
  } finally {
    loadingCaseEditor.value = false;
  }
}

async function saveCaseContent(runAfterSave = false) {
  if (!caseEditorFile.value) return;
  savingCaseEditor.value = true;
  try {
    const res = await fetch("/api/cases/content", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ case_file: caseEditorFile.value, content: caseEditorContent.value }),
    });
    const data = await res.json();
    if (data.status !== "success") { ElMessage.error(data.message || "保存失败"); return; }
    ElMessage.success("用例已保存");
    caseEditorVisible.value = false;
    if (runAfterSave) {
      inputText.value = `执行 ${data.case_file || caseEditorFile.value}`;
      startRun();
    }
  } catch (e) {
    ElMessage.error(`保存失败: ${e}`);
  } finally {
    savingCaseEditor.value = false;
  }
}

// ═══════════ 报告 ═══════════

async function loadReports() {
  try {
    const res = await fetch(`/api/reports/list?t=${Date.now()}`, { cache: "no-store" });
    const data = await res.json();
    if (data.status === "success") reportTasks.value = data.items || [];
  } catch (e) { /* ignore */ }
}

async function openReportDetail(row) {
  if (!row?.id) return;
  try {
    const res = await fetch(`/api/reports/${encodeURIComponent(row.id)}?t=${Date.now()}`, { cache: "no-store" });
    const data = await res.json();
    if (data.status !== "success") { ElMessage.error(data.message || "读取失败"); return; }
    selectedReport.value = data.report || null;
    reportDetailVisible.value = true;
  } catch (e) {
    ElMessage.error(`读取失败: ${e}`);
  }
}

// ═══════════ APP 管理 ═══════════

async function loadApps() {
  try {
    const res = await fetch('/api/apps', { cache: 'no-store' });
    const data = await res.json();
    if (data.status === 'success') appList.value = data.apps || [];
  } catch (e) { /* ignore */ }
}

function openAppDialog(row) {
  if (row) {
    appDialogMode.value = 'edit';
    appForm.value = {
      name: row.name || '',
      package: row.package || '',
      keywords: [...(row.keywords || [])],
    };
  } else {
    appDialogMode.value = 'add';
    appForm.value = { name: '', package: '', keywords: [] };
  }
  newKeyword.value = '';
  appDialogVisible.value = true;
}

function addKeyword() {
  const kw = newKeyword.value.trim();
  if (!kw) return;
  if (!appForm.value.keywords.includes(kw)) {
    appForm.value.keywords.push(kw);
  }
  newKeyword.value = '';
}

function removeKeyword(idx) {
  appForm.value.keywords.splice(idx, 1);
}

async function saveApp() {
  if (!appForm.value.name.trim()) { ElMessage.warning('请输入应用名称'); return; }
  if (!appForm.value.package.trim()) { ElMessage.warning('请输入包名'); return; }
  appSaving.value = true;
  try {
    const isEdit = appDialogMode.value === 'edit';
    const url = isEdit ? `/api/apps/${encodeURIComponent(appForm.value.package)}` : '/api/apps';
    const res = await fetch(url, {
      method: isEdit ? 'PUT' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(appForm.value),
    });
    const data = await res.json();
    if (res.ok && data.status === 'success') {
      ElMessage.success(data.message || '保存成功');
      appDialogVisible.value = false;
      loadApps();
    } else {
      ElMessage.error(data.detail || data.message || '保存失败');
    }
  } catch (e) {
    ElMessage.error(`保存失败: ${e}`);
  } finally {
    appSaving.value = false;
  }
}

async function deleteApp(row) {
  try {
    await ElMessageBox.confirm(
      `确定删除应用 "${row.name}" (${row.package})？`,
      '删除确认', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    );
  } catch { return; } // 用户取消
  try {
    const res = await fetch(`/api/apps/${encodeURIComponent(row.package)}`, { method: 'DELETE' });
    const data = await res.json();
    if (res.ok && data.status === 'success') {
      ElMessage.success(data.message || '删除成功');
      loadApps();
    } else {
      ElMessage.error(data.detail || '删除失败');
    }
  } catch (e) {
    ElMessage.error(`删除失败: ${e}`);
  }
}

// ═══════════ 知识库 ═══════════

async function loadKbCount() {
  try {
    const res = await fetch('/api/knowledge/count', { cache: 'no-store' });
    const data = await res.json();
    if (data.status === 'success') kbCount.value = data.count || 0;
  } catch (e) { /* ignore */ }
}

async function loadKbList() {
  await loadKbCount();
  const params = new URLSearchParams();
  if (kbFilterPackage.value) params.set('app_package', kbFilterPackage.value);
  if (kbFilterType.value) params.set('knowledge_type', kbFilterType.value);
  params.set('top_k', '100');
  try {
    const res = await fetch(`/api/knowledge/list?${params}`, { cache: 'no-store' });
    const data = await res.json();
    if (data.status === 'success') kbList.value = data.items || [];
  } catch (e) { /* ignore */ }
}

async function searchKb() {
  const q = kbSearchQuery.value.trim();
  if (!q) { loadKbList(); return; }
  try {
    const res = await fetch('/api/knowledge/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        query: q,
        app_package: kbFilterPackage.value,
        knowledge_type: kbFilterType.value,
        top_k: 50,
      }),
    });
    const data = await res.json();
    if (data.status === 'success') kbList.value = data.results || [];
  } catch (e) { ElMessage.error(`搜索失败: ${e}`); }
}

function resetKbFilter() {
  kbSearchQuery.value = '';
  kbFilterType.value = '';
  kbFilterPackage.value = '';
  loadKbList();
}

function openKbDialog() {
  kbForm.value = { app_package: '', knowledge_type: 'test_experience', content: '' };
  kbDialogVisible.value = true;
}

async function saveKb() {
  if (!kbForm.value.app_package.trim()) { ElMessage.warning('请输入应用包名'); return; }
  if (!kbForm.value.content.trim()) { ElMessage.warning('请输入知识内容'); return; }
  kbSaving.value = true;
  try {
    const res = await fetch('/api/knowledge', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(kbForm.value),
    });
    const data = await res.json();
    if (res.ok && data.status === 'success') {
      ElMessage.success('知识已添加');
      kbDialogVisible.value = false;
      loadKbList();
    } else {
      ElMessage.error(data.detail || data.message || '保存失败');
    }
  } catch (e) {
    ElMessage.error(`保存失败: ${e}`);
  } finally {
    kbSaving.value = false;
  }
}

function openKbDetail(row) {
  kbDetailRow.value = row;
  kbDetailVisible.value = true;
}

async function deleteKbItem(row) {
  if (!row) return;
  const pkg = (row.metadata && row.metadata.app_package) || '';
  const type = (row.metadata && row.metadata.knowledge_type) || '';
  if (!pkg && !type) { ElMessage.warning('无法定位该条知识'); return; }
  try {
    await ElMessageBox.confirm(
      `确定删除此条知识？\n包名: ${pkg || '-'}  类型: ${kbTypeLabel(type)}`,
      '删除确认', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    );
  } catch { return; }
  const params = new URLSearchParams();
  if (pkg) params.set('app_package', pkg);
  if (type) params.set('knowledge_type', type);
  try {
    const res = await fetch(`/api/knowledge?${params}`, { method: 'DELETE' });
    const data = await res.json();
    ElMessage.success(`已删除 ${data.deleted || 0} 条`);
    kbDetailVisible.value = false;
    loadKbList();
  } catch (e) {
    ElMessage.error(`删除失败: ${e}`);
  }
}

async function deleteKbByFilter() {
  if (!kbFilterPackage.value && !kbFilterType.value) {
    ElMessage.warning('请至少选择应用包名或知识类型');
    return;
  }
  const label = [kbFilterPackage.value, kbTypeLabel(kbFilterType.value)].filter(Boolean).join(' / ');
  try {
    await ElMessageBox.confirm(
      `确定批量删除 「${label}」 相关知识？此操作不可恢复。`,
      '批量删除确认', { type: 'error', confirmButtonText: '全部删除', cancelButtonText: '取消' }
    );
  } catch { return; }
  const params = new URLSearchParams();
  if (kbFilterPackage.value) params.set('app_package', kbFilterPackage.value);
  if (kbFilterType.value) params.set('knowledge_type', kbFilterType.value);
  try {
    const res = await fetch(`/api/knowledge?${params}`, { method: 'DELETE' });
    const data = await res.json();
    ElMessage.success(`已删除 ${data.deleted || 0} 条`);
    loadKbList();
  } catch (e) {
    ElMessage.error(`删除失败: ${e}`);
  }
}

function startSnapshotPolling() {
  if (snapshotTimer) return;
  if (!deviceWindowVisible.value) return;  // 投屏未打开时不启动
  snapshotTimer = window.setInterval(() => { if (executing.value) refreshSnapshot(); }, 2500);
}

function stopSnapshotPolling() {
  if (!snapshotTimer) return;
  clearInterval(snapshotTimer);
  snapshotTimer = null;
}

// ═══════════ 设备状态轮询 ═══════════

async function checkDeviceStatus() {
  try {
    const res = await fetch("/api/device/status", { cache: "no-store" });
    const data = await res.json();
    const wasOffline = !deviceOnline.value;
    deviceOnline.value = !!data.connected;
    if (data.connected && wasOffline) {
      addLog("Android 设备已连接");
      if (deviceWindowVisible.value) refreshSnapshot(true);  // 仅投屏打开时加载截图
      stopDevicePolling();
    } else if (!data.connected) {
      startDevicePolling();
    }
  } catch (e) {
    deviceOnline.value = false;
    startDevicePolling();
  }
}

function startDevicePolling() {
  if (deviceStatusTimer) return;
  devicePolling.value = true;
  deviceStatusTimer = window.setInterval(checkDeviceStatus, 3000);
}

function stopDevicePolling() {
  if (!deviceStatusTimer) return;
  clearInterval(deviceStatusTimer);
  deviceStatusTimer = null;
  devicePolling.value = false;
}

onMounted(() => {
  connectWS();
  checkDeviceStatus();
  loadReports();
  loadApps();
  loadKbList();
  window.addEventListener('resize', drawElementOverlay);
});

onBeforeUnmount(() => {
  stopSnapshotPolling();
  stopDevicePolling();
  flushStreamToken();
  window.removeEventListener('resize', drawElementOverlay);
});
</script>

<style scoped src="./App.css"></style>

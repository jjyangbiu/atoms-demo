<script setup lang="ts">
/**
 * 差异面板（Layer 6 安全网）：某版本相对前一版的文件级改动清单 + 展开看 Monaco diff。
 * 让用户在每轮迭代后核对“只改了该改的文件”，捕捉意外波及的改动（改 A 却动了 B、C）。
 * 结构仿 CodeView：左侧文件清单 + 右侧复用同一 diff 编辑器实例（避免频繁建销）。
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { ApiError } from '@/api/client'
import monaco, { languageOf } from '@/monaco'
import { useProjectStore, type DiffFileOut, type SnapshotDiffOut } from '@/stores/projects'

const props = defineProps<{ projectId: number; snapshotId: number }>()

const store = useProjectStore()
const diff = ref<SnapshotDiffOut | null>(null)
const loading = ref(false)
const errorDetail = ref('')
const selected = ref('')
const editorHost = ref<HTMLElement | null>(null)
let diffEditor: monaco.editor.IStandaloneDiffEditor | null = null
let originalModel: monaco.editor.ITextModel | null = null
let modifiedModel: monaco.editor.ITextModel | null = null

function statusLabel(status: DiffFileOut['status']): string {
  return { added: '新增', modified: '修改', removed: '删除' }[status] ?? status
}

function statusType(status: DiffFileOut['status']): 'success' | 'warning' | 'danger' {
  if (status === 'added') return 'success'
  if (status === 'removed') return 'danger'
  return 'warning'
}

function currentFile(): DiffFileOut | null {
  return diff.value?.files.find((f) => f.path === selected.value) ?? null
}

/** 把选中文件的旧/新内容灌进复用的两个 model，并对齐语法高亮语言。 */
function renderDiff() {
  const f = currentFile()
  if (!f || !originalModel || !modifiedModel) return
  originalModel.setValue(f.old)
  modifiedModel.setValue(f.new)
  const lang = languageOf(f.path)
  monaco.editor.setModelLanguage(originalModel, lang)
  monaco.editor.setModelLanguage(modifiedModel, lang)
}

function onSelect(path: string) {
  if (path === selected.value) return
  selected.value = path
  renderDiff()
}

async function load() {
  loading.value = true
  errorDetail.value = ''
  try {
    diff.value = await store.fetchSnapshotDiff(props.projectId, props.snapshotId)
    selected.value = diff.value.files[0]?.path ?? ''
    renderDiff()
  } catch (e) {
    errorDetail.value = e instanceof ApiError ? e.detail : '加载差异失败'
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  if (editorHost.value) {
    diffEditor = monaco.editor.createDiffEditor(editorHost.value, {
      readOnly: true,
      theme: 'vs',
      renderSideBySide: true,
      minimap: { enabled: false },
      automaticLayout: true,
      wordWrap: 'on',
      fontSize: 13,
    })
    originalModel = monaco.editor.createModel('', 'plaintext')
    modifiedModel = monaco.editor.createModel('', 'plaintext')
    diffEditor.setModel({ original: originalModel, modified: modifiedModel })
  }
  void load()
})

onBeforeUnmount(() => {
  originalModel?.dispose()
  modifiedModel?.dispose()
  diffEditor?.dispose()
})

watch(() => [props.projectId, props.snapshotId] as const, () => void load())
</script>

<template>
  <div class="diff-panel">
    <div v-if="diff" class="diff-header" data-testid="diff-header">
      <span class="diff-range">
        版本 {{ diff.base_rev ?? '初版' }} → {{ diff.target_rev }}
      </span>
      <el-tag size="small" type="info" effect="plain">
        {{ diff.files.length }} 个文件改动
      </el-tag>
    </div>
    <div class="diff-body">
      <ul v-if="diff && diff.files.length" class="diff-list" data-testid="diff-file-list">
        <li v-for="f in diff.files" :key="f.path">
          <button
            type="button"
            class="diff-item"
            :class="{ active: f.path === selected }"
            :data-testid="`diff-file-${f.path}`"
            @click="onSelect(f.path)"
          >
            <el-tag size="small" :type="statusType(f.status)" effect="dark">
              {{ statusLabel(f.status) }}
            </el-tag>
            <span class="diff-path">{{ f.path }}</span>
          </button>
        </li>
      </ul>
      <div class="diff-main">
        <div v-show="selected" ref="editorHost" class="diff-editor" data-testid="diff-editor" />
        <div v-if="loading" class="diff-overlay">加载中…</div>
        <div v-else-if="errorDetail" class="diff-overlay error">{{ errorDetail }}</div>
        <el-empty
          v-else-if="diff && !diff.files.length"
          description="本版本无文件改动"
          :image-size="48"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.diff-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

.diff-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  font-size: 13px;
  color: #606266;
  border-bottom: 1px solid #ebeef5;
}

.diff-range {
  font-weight: 600;
  color: #303133;
}

.diff-body {
  flex: 1;
  display: flex;
  min-height: 0;
}

.diff-list {
  width: 220px;
  min-width: 180px;
  list-style: none;
  margin: 0;
  padding: 4px;
  border-right: 1px solid #e4e7ed;
  background: #fafafa;
  overflow-y: auto;
}

.diff-item {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 6px 8px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  text-align: left;
  font-size: 12px;
  color: #303133;
  cursor: pointer;
}

.diff-item:hover {
  background: #ecf5ff;
}

.diff-item.active {
  background: #d9ecff;
}

.diff-path {
  font-family: 'Cascadia Code', Consolas, monospace;
  word-break: break-all;
}

.diff-main {
  flex: 1;
  position: relative;
  min-width: 0;
  display: flex;
}

.diff-editor {
  flex: 1;
  min-height: 0;
}

.diff-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(255, 255, 255, 0.9);
  font-size: 13px;
  color: #909399;
}

.diff-overlay.error {
  color: #f56c6c;
}
</style>

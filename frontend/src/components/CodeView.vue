<script setup lang="ts">
/**
 * 代码视图：文件树 + Monaco 只读高亮（工单 0007）。
 * 内容经属主鉴权的文件内容接口获取，评审者可核查真实生成代码。
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { api, ApiError } from '@/api/client'
import monaco, { languageOf } from '@/monaco'
import type { FileOut } from '@/stores/projects'

// 配置位：当前代码视图固定只读；后续若开放在线编辑，改为由设置/项目配置驱动
const CODE_EDITOR_READONLY = true

interface FileContent {
  path: string
  size: number
  content: string
}

const props = defineProps<{ projectId: number; files: FileOut[]; version: number }>()

const selected = ref('')
const loading = ref(false)
const errorDetail = ref('')
const editorHost = ref<HTMLElement | null>(null)
let editor: monaco.editor.IStandaloneCodeEditor | null = null
let model: monaco.editor.ITextModel | null = null

function pathToUrl(path: string): string {
  return path.split('/').map(encodeURIComponent).join('/')
}

async function loadContent(path: string) {
  loading.value = true
  errorDetail.value = ''
  try {
    const data = await api<FileContent>(`/api/projects/${props.projectId}/files/${pathToUrl(path)}`)
    if (path !== selected.value) return // 加载期间已切换文件，丢弃过期结果
    if (model) {
      model.setValue(data.content)
      monaco.editor.setModelLanguage(model, languageOf(path))
    }
  } catch (e) {
    errorDetail.value = e instanceof ApiError ? e.detail : '加载文件失败'
  } finally {
    loading.value = false
  }
}

function onSelect(path: string) {
  if (path === selected.value) return
  selected.value = path
  void loadContent(path)
}

/** 文件清单变化（生成/回滚）后对齐选中项并重新加载内容。 */
function syncSelection() {
  if (!props.files.some((f) => f.path === selected.value)) {
    selected.value = props.files[0]?.path ?? ''
  }
  if (selected.value) void loadContent(selected.value)
}

onMounted(() => {
  if (editorHost.value) {
    editor = monaco.editor.create(editorHost.value, {
      readOnly: CODE_EDITOR_READONLY,
      theme: 'vs',
      minimap: { enabled: false },
      automaticLayout: true,
      wordWrap: 'on',
      fontSize: 13,
      renderLineHighlight: 'none',
    })
    model = monaco.editor.createModel('', 'plaintext')
    editor.setModel(model)
  }
  syncSelection()
})

onBeforeUnmount(() => {
  model?.dispose()
  editor?.dispose()
})

watch(
  () => [props.files, props.version] as const,
  () => syncSelection(),
)
</script>

<template>
  <div class="code-view">
    <aside class="file-tree">
      <div class="file-tree-header">
        <span>文件</span>
        <el-tag size="small" type="info" effect="plain">只读</el-tag>
      </div>
      <el-empty v-if="!files.length" description="暂无文件" :image-size="48" />
      <ul v-else class="file-list" data-testid="file-tree">
        <li v-for="f in files" :key="f.path">
          <button
            type="button"
            class="file-item"
            :class="{ active: f.path === selected }"
            :data-testid="`file-item-${f.path}`"
            @click="onSelect(f.path)"
          >
            {{ f.path }}
          </button>
        </li>
      </ul>
    </aside>
    <div class="code-main">
      <div v-show="files.length" ref="editorHost" class="editor-host" data-testid="code-editor" />
      <div v-if="loading" class="code-overlay">加载中…</div>
      <div v-else-if="errorDetail" class="code-overlay error">{{ errorDetail }}</div>
      <el-empty v-if="!files.length" description="代码区：生成完成的文件内容将在这里展示" />
    </div>
  </div>
</template>

<style scoped>
.code-view {
  flex: 1;
  display: flex;
  min-height: 0;
}

.file-tree {
  width: 200px;
  min-width: 160px;
  border-right: 1px solid #e4e7ed;
  background: #fafafa;
  display: flex;
  flex-direction: column;
  overflow-y: auto;
}

.file-tree-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  font-size: 12px;
  color: #909399;
  border-bottom: 1px solid #ebeef5;
}

.file-list {
  list-style: none;
  margin: 0;
  padding: 4px;
}

.file-item {
  display: block;
  width: 100%;
  padding: 6px 8px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  text-align: left;
  font-size: 12px;
  font-family: 'Cascadia Code', Consolas, monospace;
  color: #303133;
  cursor: pointer;
  word-break: break-all;
}

.file-item:hover {
  background: #ecf5ff;
}

.file-item.active {
  background: #d9ecff;
  color: #409eff;
}

.code-main {
  flex: 1;
  position: relative;
  min-width: 0;
  display: flex;
}

.editor-host {
  flex: 1;
  min-height: 0;
}

.code-overlay {
  position: absolute;
  inset: auto 0 0 0;
  padding: 4px 12px;
  background: rgba(255, 255, 255, 0.9);
  border-top: 1px solid #ebeef5;
  font-size: 12px;
  color: #909399;
}

.code-overlay.error {
  color: #f56c6c;
}
</style>

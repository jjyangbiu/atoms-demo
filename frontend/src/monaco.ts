/**
 * Monaco 编辑器初始化：Worker 注册（Vite ?worker 导入）。
 * 全应用共享同一份 monaco 实例，代码视图按需引用（工单 0007）。
 */
import * as monaco from 'monaco-editor'
import editorWorker from 'monaco-editor/editor/editor.worker.js?worker'
import cssWorker from 'monaco-editor/language/css/css.worker.js?worker'
import htmlWorker from 'monaco-editor/language/html/html.worker.js?worker'
import jsonWorker from 'monaco-editor/language/json/json.worker.js?worker'
import tsWorker from 'monaco-editor/language/typescript/ts.worker.js?worker'

self.MonacoEnvironment = {
  getWorker(_workerId: string, label: string) {
    if (label === 'css' || label === 'scss' || label === 'less') return new cssWorker()
    if (label === 'html' || label === 'handlebars' || label === 'razor') return new htmlWorker()
    if (label === 'json') return new jsonWorker()
    if (label === 'typescript' || label === 'javascript') return new tsWorker()
    return new editorWorker()
  },
}

declare global {
  interface Window {
    MonacoEnvironment?: monaco.Environment
  }
}

const LANGUAGES: Record<string, string> = {
  html: 'html',
  css: 'css',
  js: 'javascript',
  mjs: 'javascript',
  json: 'json',
  md: 'markdown',
  svg: 'xml',
  txt: 'plaintext',
}

/** 按文件扩展名映射 Monaco 语言，用于只读语法高亮。 */
export function languageOf(path: string): string {
  const ext = path.slice(path.lastIndexOf('.') + 1).toLowerCase()
  return LANGUAGES[ext] ?? 'plaintext'
}

export default monaco

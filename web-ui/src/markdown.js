import DOMPurify from 'dompurify'
import { marked } from 'marked'

// markdown → 消毒后 HTML 的单点：产物与消息流共用（内容都系 agent 转述
// 外部文档/工具输出，同威胁模型，HTML 一律消毒再进 DOM）
export const mdToHtml = (text) => DOMPurify.sanitize(marked.parse(text, { async: false }))

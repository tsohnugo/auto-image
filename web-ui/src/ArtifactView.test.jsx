import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { sanitize } = vi.hoisted(() => ({
  sanitize: vi.fn((html) => html.replace(/ onerror="[^"]*"/g, '')),
}))
vi.mock('dompurify', () => ({ default: { sanitize } }))

import ArtifactView from './components/ArtifactView.jsx'

const rpm = {
  binary: true,
  dir: 'rpm/nginx/1.25.3/rpms/binary',
  name: 'nginx-1.25.3-1.aarch64.rpm',
  size: 12 * 1024 * 1024,
  stage: 'BUILD',
}
const onDownload = () => {}

beforeEach(() => {
  sanitize.mockClear()
})

describe('产物视图', () => {
  it('未加载时只显示加载占位', () => {
    const html = renderToStaticMarkup(
      <ArtifactView artifact={null} onDownload={onDownload} />,
    )

    expect(html).toContain('加载中…')
    expect(html).not.toContain('<button')
    expect(sanitize).not.toHaveBeenCalled()
  })

  it('无 content 的 RPM 显示不可预览占位和下载动作', () => {
    const render = () => renderToStaticMarkup(
      <ArtifactView artifact={rpm} onDownload={onDownload} />,
    )

    expect(render).not.toThrow()
    const html = render()
    expect(html).toContain(rpm.name)
    expect(html).toContain('12.0 MB')
    expect(html).toContain('不支持在线预览')
    expect(html).toContain('下载此文件')
    expect(html.match(/<button/g)).toHaveLength(2)
    expect(sanitize).not.toHaveBeenCalled()
  })

  it('二进制标记优先于 JSON 文件名', () => {
    const html = renderToStaticMarkup(
      <ArtifactView
        artifact={{ ...rpm, name: 'package.json' }}
        onDownload={onDownload}
      />,
    )

    expect(html).toContain('二进制产物')
    expect(html).not.toContain('va-artifact-raw')
    expect(sanitize).not.toHaveBeenCalled()
  })

  it('Markdown 经消毒后渲染并保留下载动作', () => {
    const html = renderToStaticMarkup(
      <ArtifactView
        artifact={{
          dir: 'deploy/nginx/1.25',
          name: 'nginx-install.md',
          stage: 'GUIDE',
          content: '**安全内容**\n\n<img src="x" onerror="alert(1)">',
        }}
        onDownload={onDownload}
      />,
    )

    expect(sanitize).toHaveBeenCalledOnce()
    expect(sanitize.mock.calls[0][0]).toContain('onerror=')
    expect(html).toContain('<strong>安全内容</strong>')
    expect(html).not.toContain('onerror=')
    expect(html).toContain('title="下载此文件"')
  })

  it('JSON 保留原文且不进入 Markdown 消毒器', () => {
    const content = '{"z": 1,\n  "a": "<原文>"}'
    const html = renderToStaticMarkup(
      <ArtifactView
        artifact={{
          dir: 'deploy/nginx/1.25',
          name: 'nginx-install-meta.json',
          stage: 'INSTALL',
          content,
        }}
        onDownload={onDownload}
      />,
    )

    expect(html).toContain('{&quot;z&quot;: 1,\n  &quot;a&quot;: &quot;&lt;原文&gt;&quot;}')
    expect(html).toContain('title="下载此文件"')
    expect(sanitize).not.toHaveBeenCalled()
  })
})

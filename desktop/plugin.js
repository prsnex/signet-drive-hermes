// Signet Drive for Hermes, the Desktop half: a status bar item that is always
// present, and the drive's view opened by clicking it. There is no docked
// pane, because closing a plugin's only pane switches off its Desktop half
// and the warning would go with it. The view only reads: its two answers come
// from the plugin's own backend, /status and /files, and nothing here can
// save, fetch, restore or delete.
import { host, haptic } from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'signet-drive'
const VIEW_ID = 'signet-drive-view'
const POLL_MS = 60000

function useStatus(ctx) {
  const [status, setStatus] = useState(null)
  useEffect(() => {
    let live = true
    const load = () => ctx.rest('/status').then(s => live && setStatus(s), () => live && setStatus({ state: 'unknown', word: 'Drive not checked', line: 'The plugin backend did not answer.' }))
    load()
    const timer = setInterval(load, POLL_MS)
    const off = host.onEvent(`plugin.${ID}.status.changed`, ({ payload }) => live && payload && setStatus(payload))
    return () => { live = false; clearInterval(timer); if (typeof off === 'function') off() }
  }, [ctx])
  return status
}

function StatusChip({ ctx }) {
  const status = useStatus(ctx)
  const word = status ? status.word : 'Drive…'
  const tone = status && status.state === 'needs_you' ? 'text-(--ui-text-error)' : 'text-(--ui-text-tertiary)'
  return jsx('button', {
    type: 'button',
    className: `px-1.5 text-[0.6875rem] ${tone}`,
    title: status ? status.line : 'Signet Drive',
    onClick: () => {
      haptic('tap')
      if (typeof host.openWorkspace === 'function') {
        host.openWorkspace(VIEW_ID, { render: () => jsx(DriveView, { ctx }), title: 'Signet Drive', minWidth: 360 })
      } else {
        host.notify({ kind: 'info', message: status ? status.line : 'Signet Drive, not checked yet.' })
      }
    },
    children: word
  })
}

function fmtStamp(stamp) {
  // 20261107T165612Z-1e0bfd -> local date and time
  const m = /(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z/.exec(stamp || '')
  if (!m) return stamp
  const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]))
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function fmtBytes(n) {
  if (n == null) return ''
  if (n < 1024) return `${n} B`
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1048576).toFixed(1)} MB`
}

function humanLine(status) {
  // A few words for the human; the full line is the tooltip.
  const c = status.last_check || {}
  const by = {}
  for (const x of (c.checks || [])) by[x.name] = x
  const bits = []
  if (c.at) bits.push(`Checked ${new Date(c.at).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}.`)
  if (by.client_genuine && by.client_genuine.ok === true) bits.push('Client genuine.')
  else if (by.client_genuine && by.client_genuine.ok === false) bits.push('Client not genuine.')
  if (by.enrolled && by.enrolled.ok) bits.push(`Enrolled as ${c.handle}.`)
  if (by.key_matches_log) {
    const k = by.key_matches_log
    bits.push(k.ok === true ? 'Key matches the public log.' : k.ok === false ? 'Key check failed.' : (k.detail || '').startsWith('OVERDUE') ? 'Key not yet published, overdue.' : 'Key not yet published.')
  }
  if (by.drive && by.drive.ok) bits.push('Drive reachable.')
  else if (by.drive) bits.push('Drive not reachable.')
  const p = status.last_pass
  if (p && p.at) bits.push(`Last copy ${new Date(p.at).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}, ${p.status === 'in_step' ? 'in step' : p.status === 'uploaded' ? `${p.uploaded} file(s) copied` : p.status === 'needs_you' ? 'needs you' : p.status}.`)
  return bits.join(' ')
}

function DriveView({ ctx }) {
  const status = useStatus(ctx)
  const [files, setFiles] = useState(null)
  useEffect(() => {
    let live = true
    ctx.rest('/files').then(f => live && setFiles(f), e => live && setFiles({ files: [], copies: [], error: String(e) }))
    return () => { live = false }
  }, [ctx, status && status.last_pass && status.last_pass.at])
  const serverUrl = (status && status.config && status.config.server_url) || 'https://drive.mysignet.ca'
  const rows = files && files.files ? files.files : []
  const copies = files && files.copies ? files.copies : []
  return jsxs('div', {
    className: 'flex h-full flex-col gap-3 p-3 text-sm',
    children: [
      jsx('div', { className: 'font-medium', children: status ? status.word : 'Signet Drive' }),
      jsx('div', { className: 'text-(--ui-text-secondary)', title: status ? status.line : '', children: status ? humanLine(status) : 'Loading…' }),
      jsxs('div', {
        className: 'flex flex-col gap-1',
        children: [
          jsx('div', { className: 'font-medium', children: 'Files' }),
          files == null ? jsx('div', { className: 'text-(--ui-text-tertiary)', children: 'Loading…' })
            : files.error ? jsx('div', { className: 'text-(--ui-text-tertiary)', children: `Not readable now (${files.error})` })
            : rows.length === 0 ? jsx('div', { className: 'text-(--ui-text-tertiary)', children: 'No files yet.' })
            : jsx('ul', {
                className: 'flex flex-col gap-0.5',
                children: rows.map(r => jsxs('li', {
                  className: 'flex justify-between gap-3',
                  children: [
                    jsx('span', { children: r.name }),
                    jsx('span', { className: 'text-(--ui-text-tertiary)', children: `${fmtBytes(r.size_bytes)} ${r.modified_at || ''}`.trim() })
                  ]
                }, r.name))
              })
        ]
      }),
      jsxs('div', {
        className: 'flex flex-col gap-1',
        children: [
          jsx('div', { className: 'font-medium', children: 'Restore points' }),
          copies.length === 0 ? jsx('div', { className: 'text-(--ui-text-tertiary)', children: 'No restore points yet.' })
            : jsx('ul', { className: 'flex flex-col gap-0.5', children: copies.slice().reverse().map(c => jsxs('li', { className: 'flex gap-3', title: c, children: [jsx('span', { children: fmtStamp(c) }), jsx('span', { className: 'text-(--ui-text-tertiary)', children: /pinned/i.test(c) ? 'pinned' : '' })] }, c)) })
        ]
      }),
      jsx('div', { className: 'text-(--ui-text-tertiary)', children: 'This view only reads. Deleting a file or a copy is permanent, and it is done in your own drive in the browser.' }),
      jsx('button', {
        type: 'button',
        className: 'self-start rounded border border-(--ui-border) px-2 py-1',
        onClick: () => ctx.os.openExternal(serverUrl),
        children: 'Open my drive in the browser'
      })
    ]
  })
}

export default {
  id: ID, // must match the folder name
  name: 'Signet Drive',
  register(ctx) {
    ctx.register({
      id: 'status-item',
      area: 'statusBar.right',
      order: 125,
      render: () => jsx(StatusChip, { ctx })
    })
  }
}

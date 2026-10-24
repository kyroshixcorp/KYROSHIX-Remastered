import { useState, useEffect, useCallback } from 'react'
import Card from '../components/Card'
import { api } from '../lib/api'
import { formatBytes, formatUptime, formatNumber } from '../lib/utils'
import type { AppState } from '../lib/types'
import {
  RiMemoryLine, RiCpuLine, RiHardDriveLine, RiNetworkChartLine,
  RiRefreshLine, RiErrorWarningLine, RiCheckCircleLine, RiAlertLine,
  RiGpuLine, RiTimerLine,
} from 'react-icons/ri'

interface Props {
  state: AppState | null
  onToast: (msg: string, level?: string) => void
}

export default function SystemHealth({ state, onToast }: Props) {
  const [health, setHealth] = useState<{
    uptime_seconds: number
    ram: { total_bytes: number; available_bytes: number; used_bytes: number; free_bytes: number; percent: number | null }
    gpu: { available: boolean; vendor: string | null; name: string | null; vram_total_bytes: number | null; driver: string | null }
  } | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const h = await api<{ ok: boolean; uptime_seconds: number; ram: any; gpu: any; session: any }>('/api/health')
      setHealth({
        uptime_seconds: h.uptime_seconds,
        ram: h.ram,
        gpu: h.gpu,
      })
    } catch (e: unknown) {
      setError((e as Error).message)
      onToast((e as Error).message, 'error')
    }
    setLoading(false)
  }, [onToast])

  useEffect(() => { load() }, [load])

  const ramPercent = health?.ram.percent
  const gpu = health?.gpu

  return (
    <div className="flex flex-col h-[calc(100vh-3.5rem)]">
      {/* Header */}
      <div className="px-4 pt-4 pb-2 shrink-0 border-b border-white/[0.06]">
        <div className="flex items-center gap-2">
          <RiTimerLine className="text-accent" size={18} />
          <h2 className="font-title text-lg text-text">System Health</h2>
        </div>
        <p className="text-text-muted/50 text-xs font-title mt-0.5">
          RAM, GPU and session health for 24/7 uptime monitoring.
        </p>
      </div>

      {/* Stats grid */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3 px-4 py-2 shrink-0">
        <Card>
          <div className="flex items-center gap-3">
            <div className="p-2 rounded-lg bg-mint/10 text-mint">
              <RiMemoryLine size={20} />
            </div>
            <div>
              <div className="text-[10px] text-text-muted/50 font-title uppercase">RAM Usage</div>
              <div className="font-title text-lg text-text">
                {health?.ram.percent != null ? `${health.ram.percent}%` : '—'}
              </div>
              <div className="text-text-muted/40 text-[10px] font-title">
                {formatBytes(health?.ram.used_bytes)} / {formatBytes(health?.ram.total_bytes)}
              </div>
            </div>
          </div>
          {health?.ram.percent != null && (
            <div className="mt-2 h-[6px] bg-background rounded-full overflow-hidden">
              <div
                className="h-full bg-accent rounded-full transition-[width] duration-500"
                style={{ width: `${Math.min(100, health.ram.percent)}%` }}
              />
            </div>
          )}
        </Card>

        <Card>
          <div className="flex items-center gap-3">
            <div className="p-2 rounded-lg bg-accent/10 text-accent">
              <RiGpuLine size={20} />
            </div>
            <div>
              <div className="text-[10px] text-text-muted/50 font-title uppercase">GPU</div>
              <div className="font-title text-lg text-text">
                {gpu?.name || (gpu?.available ? 'Detected' : 'Not Available')}
              </div>
              <div className="text-text-muted/40 text-[10px] font-title">
                {gpu?.vendor || '—'} • {gpu?.driver || '—'}
              </div>
            </div>
            {gpu?.vram_total_bytes && (
              <div className="ml-auto text-right">
                <div className="text-text-muted/40 text-[10px] font-title">VRAM</div>
                <div className="text-text font-title">{formatBytes(gpu.vram_total_bytes)}</div>
              </div>
            )}
          </div>
        </Card>

        <Card>
          <div className="flex items-center gap-3">
            <div className="p-2 rounded-lg bg-accent/10 text-accent">
              <RiNetworkChartLine size={20} />
            </div>
            <div>
              <div className="text-[10px] text-text-muted/50 font-title uppercase">Uptime</div>
              <div className="font-title text-lg text-text">
                {formatUptime(health?.uptime_seconds)}
              </div>
              <div className="text-text-muted/40 text-[10px] font-title">
                Session: {state?.session_handle?.exists ? 'Active' : 'Inactive'}
              </div>
            </div>
          </div>
        </Card>
      </div>

      {/* Details panel */}
      <div className="flex-1 overflow-y-auto px-4 py-2">
        {/* GPU details */}
        <Card className="mb-3">
          <div className="flex items-center gap-2 mb-3">
            <RiHardDriveLine className="text-text-muted" size={16} />
            <h3 className="font-title text-sm text-text">GPU Details</h3>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            <DetailRow label="Available" value={gpu?.available ? 'Yes' : 'No'} active={gpu?.available} />
            <DetailRow label="Vendor" value={gpu?.vendor || '—'} />
            <DetailRow label="Name" value={gpu?.name || '—'} />
            <DetailRow label="Driver" value={gpu?.driver || '—'} />
            <DetailRow label="VRAM Total" value={gpu?.vram_total_bytes ? formatBytes(gpu.vram_total_bytes) : '—'} />
          </div>
          {!gpu?.available && (
            <div className="mt-3 p-2 rounded bg-rose/10 text-rose text-xs">
              <RiErrorWarningLine size={14} className="mr-1" />
              No GPU detected via rocm-smi or torch.cuda. Ensure AMD ROCm drivers are loaded.
            </div>
          )}
        </Card>

        {/* RAM details */}
        <Card className="mb-3">
          <div className="flex items-center gap-2 mb-3">
            <RiMemoryLine className="text-text-muted" size={16} />
            <h3 className="font-title text-sm text-text">Memory Details</h3>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            <DetailRow label="Total" value={formatBytes(health?.ram.total_bytes)} />
            <DetailRow label="Used" value={formatBytes(health?.ram.used_bytes)} />
            <DetailRow label="Available" value={formatBytes(health?.ram.available_bytes)} />
            <DetailRow label="Free" value={formatBytes(health?.ram.free_bytes)} />
          </div>
          <p className="mt-2 text-text-muted/50 text-[10px] font-title">
            RAM percent: {health?.ram.percent != null ? `${health.ram.percent}%` : '—'}
          </p>
        </Card>

        {/* Session health */}
        <Card>
          <div className="flex items-center gap-2 mb-3">
            <RiCheckCircleLine className="text-text-muted" size={16} />
            <h3 className="font-title text-sm text-text">Session Health</h3>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            <DetailRow label="Connected" value={state?.is_connected ? 'Yes' : 'No'} active={state?.is_connected} />
            <DetailRow label="Session Handle" value={state?.session_handle?.exists ? 'Active' : 'Inactive'} active={state?.session_handle?.exists} />
            <DetailRow label="Handle Age" value={state?.session_handle?.age_minutes != null ? `${Math.round(state.session_handle.age_minutes)}m` : '—'} />
            <DetailRow label="Prompt Tokens" value={formatNumber(state?.usage_metadata?.prompt_tokens)} />
            <DetailRow label="Response Tokens" value={formatNumber(state?.usage_metadata?.response_tokens)} />
            <DetailRow label="Tool Calls" value={formatNumber(state?.usage_metadata?.tool_calls)} />
          </div>
        </Card>

        {error && (
          <div className="mt-3 p-3 rounded bg-rose/10 border border-rose/20 text-rose text-sm">
            <RiAlertLine size={14} className="mr-1" />
            Failed to load health data: {error}
          </div>
        )}
      </div>

      {/* Refresh button */}
      <div className="px-4 py-2 shrink-0 border-t border-white/[0.06]">
        <button
          onClick={load}
          disabled={loading}
          className="flex items-center gap-1.5 w-full px-3 py-1.5 bg-accent text-background font-title text-sm rounded-lg hover:bg-accent-dim disabled:opacity-50 transition-colors"
        >
          {loading ? <RiRefreshLine className="animate-spin" size={14} /> : <RiRefreshLine size={14} />}
          Refresh
        </button>
      </div>
    </div>
  )
}

function DetailRow({ label, value, active }: { label: string; value: string; active?: boolean }) {
  return (
    <div className="bg-white/[0.02] border border-white/[0.05] rounded-lg p-2">
      <div className="text-[10px] text-text-muted/50 font-title uppercase">{label}</div>
      <div className={`font-title text-sm ${active ? 'text-mint' : 'text-text/90'}`}>{value}</div>
    </div>
  )
}

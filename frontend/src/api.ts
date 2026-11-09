export interface Session {
  actor: string
  role: string
  csrf_token: string
}

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message) }
}

let session: Session | null = null

export function setSession(value: Session | null) { session = value }

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  return request<T>(`/api/v1${path}`, options)
}

export async function publicApi<T>(path: string, options: RequestInit = {}): Promise<T> {
  return request<T>(`/api/public/v1${path}`, options)
}

async function request<T>(path: string, options: RequestInit): Promise<T> {
  const headers = new Headers(options.headers)
  if (typeof options.body === 'string') headers.set('Content-Type', 'application/json')
  if (session && options.method && options.method !== 'GET') headers.set('X-CSRF-Token', session.csrf_token)
  const response = await fetch(path, { ...options, headers })
  if (!response.ok) {
    const problem = await response.json().catch(() => ({}))
    throw new ApiError(response.status, problem.title ?? `请求失败（${response.status}）`)
  }
  return (response.status === 204 ? undefined : await response.json()) as T
}

export async function upload(domain: string, type: string, file: File) {
  const body = new FormData()
  body.set('source_type', type)
  body.set('file', file)
  const response = await api<{ file_id: string }>(`/attendance/${domain}/source-files`, { method: 'POST', body })
  return response.file_id
}

export interface Job {
  status: 'queued' | 'running' | 'succeeded' | 'failed'
  result: Record<string, string> | null
  error: string | null
}

export async function awaitJob(id: string, onState: (job: Job) => void): Promise<Record<string, string>> {
  for (;;) {
    const job = await api<Job>(`/jobs/${id}`)
    onState(job)
    if (job.status === 'succeeded' && job.result) return job.result
    if (job.status === 'failed') throw new Error(job.error ?? '计算任务失败')
    await new Promise(resolve => setTimeout(resolve, 500))
  }
}

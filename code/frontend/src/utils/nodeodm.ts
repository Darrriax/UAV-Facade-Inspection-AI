/**
 * A browser client for NodeODM's REST API, used by the 3D page.
 *
 * The production pipeline talks to NodeODM from the Celery worker, which
 * imposes its own deadline on each task. This client runs in the browser
 * instead, so the page can follow a long reconstruction for as long as it
 * takes and show ODM's progress as it happens.
 *
 * A task is opened, filled and committed as three calls rather than one: a
 * sortie is gigabytes, so the caller hands in one batch at a time and drops
 * each batch once it is sent. ODM's own API is shaped that way, and a task that
 * is never committed never starts - which is also what makes an abandoned run
 * cost nothing but the upload.
 *
 * NodeODM serves only `all.zip` from the download endpoint, so the textured
 * `.glb` (produced by the `gltf` option) is extracted from that archive here.
 */

import JSZip from 'jszip'

/** NodeODM's status codes, from its `/task/:uuid/info` response. */
export const NODEODM_STATUS = {
  queued: 10,
  running: 20,
  failed: 30,
  completed: 40,
  canceled: 50,
} as const

export interface NodeOdmTaskInfo {
  uuid: string
  name: string
  status: { code: number; errorMessage?: string }
  progress: number
}

/** Options accepted by NodeODM. Each becomes an entry in the task's option list. */
export type NodeOdmOptions = Record<string, string | number | boolean>

export class NodeOdmError extends Error {}

export class NodeOdmClient {
  private readonly baseUrl: string

  constructor(baseUrl: string) {
    this.baseUrl = baseUrl.replace(/\/+$/, '')
  }

  /**
   * Opens a task with its options and returns its uuid.
   *
   * The task does nothing until `commitTask`: ODM only queues it once the caller
   * says the images are all in, so opening a task that is never filled leaves
   * the queue untouched.
   */
  async createTask(name: string, options: NodeOdmOptions): Promise<string> {
    const initForm = new FormData()
    initForm.append('name', name)
    initForm.append(
      'options',
      JSON.stringify(Object.entries(options).map(([optionName, value]) => ({ name: optionName, value }))),
    )
    const init = await this.request<{ uuid: string }>('/task/new/init', { method: 'POST', body: initForm })
    return init.uuid
  }

  /**
   * Adds one batch of images to an open task.
   *
   * Repeatable, and the reason uploading a sortie does not need the whole sortie
   * in memory: the caller can release each batch as soon as this returns.
   */
  async uploadImages(uuid: string, images: readonly File[]): Promise<void> {
    const form = new FormData()
    for (const image of images) {
      form.append('images', image, image.name)
    }
    await this.request(`/task/new/upload/${uuid}`, { method: 'POST', body: form })
  }

  /** Hands the task to ODM: it starts as soon as it reaches the front of the queue. */
  async commitTask(uuid: string): Promise<void> {
    await this.request(`/task/new/commit/${uuid}`, { method: 'POST' })
  }

  async getTaskInfo(uuid: string): Promise<NodeOdmTaskInfo> {
    return this.request<NodeOdmTaskInfo>(`/task/${uuid}/info`)
  }

  /** Asks NodeODM to stop the reconstruction. Removing the task would not stop ODM. */
  async cancelTask(uuid: string): Promise<void> {
    await this.request('/task/cancel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ uuid }),
    })
  }

  /**
   * Downloads the finished reconstruction and returns its textured `.glb`.
   * Throws if the archive holds no GLB, which means the `gltf` option was not
   * set or ODM did not reach the texturing stage.
   */
  async fetchGlb(uuid: string): Promise<Blob> {
    const response = await fetch(`${this.baseUrl}/task/${uuid}/download/all.zip`)
    if (!response.ok) {
      throw new NodeOdmError(`NodeODM returned HTTP ${response.status} for the result archive`)
    }
    const archive = await JSZip.loadAsync(await response.arrayBuffer())
    const glbEntries = Object.values(archive.files).filter((entry) => !entry.dir && entry.name.endsWith('.glb'))
    if (glbEntries.length === 0) {
      throw new NodeOdmError('The result archive contains no .glb file')
    }
    // Prefer the georeferenced mesh ODM writes last; any GLB is still a model.
    const preferred =
      glbEntries.find((entry) => entry.name.endsWith('odm_textured_model_geo.glb')) ?? glbEntries[0]
    if (!preferred) {
      throw new NodeOdmError('The result archive contains no .glb file')
    }
    return preferred.async('blob')
  }

  private async request<T = unknown>(path: string, init?: RequestInit): Promise<T> {
    let response: Response
    try {
      response = await fetch(`${this.baseUrl}${path}`, init)
    } catch {
      throw new NodeOdmError(`Could not reach NodeODM at ${this.baseUrl}`)
    }
    const body = (await response.json().catch(() => null)) as { error?: string } | T | null
    if (!response.ok || (body && typeof body === 'object' && 'error' in body && body.error)) {
      const message = body && typeof body === 'object' && 'error' in body ? body.error : null
      throw new NodeOdmError(message ?? `NodeODM returned HTTP ${response.status}`)
    }
    return body as T
  }
}

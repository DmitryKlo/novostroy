export type LayerFailure = {
  url: string
  kind: string
  label: string
  phase: 'root' | 'content'
  count: number
  message: string
  errorClass: string
  contentUrl: string | null
}

function failureDetail(error: unknown): { message: string; url: string | null; errorClass: string } {
  if (error && typeof error === 'object') {
    const detail = error as { message?: unknown; url?: unknown; name?: unknown }
    return {
      message: typeof detail.message === 'string' ? detail.message : String(error),
      url: typeof detail.url === 'string' ? detail.url : null,
      errorClass: typeof detail.name === 'string' ? detail.name : error.constructor?.name ?? 'Object',
    }
  }
  return { message: String(error), url: null, errorClass: typeof error }
}

/** A loaded tileset root does not imply its GLB content loaded successfully. */
export class MapTileHealth {
  private readonly failures = new Map<string, LayerFailure>()

  mark(tile: { url: string; kind: string; label: string }, phase: 'root' | 'content', error: unknown): number {
    const detail = failureDetail(error)
    const previous = this.failures.get(tile.url)
    this.failures.set(tile.url, {
      url: tile.url, kind: tile.kind, label: tile.label, phase,
      count: (previous?.count ?? 0) + 1, message: detail.message, errorClass: detail.errorClass,
      contentUrl: detail.url ?? previous?.contentUrl ?? null,
    })
    return (previous?.count ?? 0) + 1
  }

  active(urls: Iterable<string>): LayerFailure[] {
    return [...urls].map((url) => this.failures.get(url)).filter((failure): failure is LayerFailure => Boolean(failure))
  }

  clear(): void { this.failures.clear() }
}

export function layerLoadText(connectedRoots: number, total: number, failures: readonly LayerFailure[]): string {
  if (failures.length) return `Ошибка загрузки тайлов: ${failures.map((failure) => failure.label).join(', ')}`
  return `Подключено ${connectedRoots}/${total} наборов`
}

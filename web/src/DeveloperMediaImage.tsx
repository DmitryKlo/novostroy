import { useState } from 'react'
import type { DeveloperMedia } from './api'

export function developerMediaUrl(media: DeveloperMedia): string | null {
  if (!/^media_[0-9a-f]{24}$/.test(media.media_id)) return null
  return media.asset_url === `/v1/developer-media/${media.media_id}` ? media.asset_url : null
}

export function projectMediaLabel(media: DeveloperMedia): string {
  return media.scope === 'project_gallery_single_corpus' && media.role === 'facade_photo' ? 'Фото ЖК' : 'Визуализация ЖК'
}

export default function DeveloperMediaImage({ media, label, className = '' }: { media: DeveloperMedia; label: string; className?: string }) {
  const [failed, setFailed] = useState(false)
  const [retried, setRetried] = useState(false)
  const url = developerMediaUrl(media)
  return <figure className={className}>
    {url && !failed ? <img src={retried ? `${url}?retry=1` : url} alt={label} loading="lazy" onError={() => setFailed(true)} />
      : <div className="developer-media-unavailable">Изображение недоступно{url && !retried && <button type="button" onClick={() => { setRetried(true); setFailed(false) }}>Повторить</button>}</div>}
    <figcaption>{label}</figcaption>
  </figure>
}

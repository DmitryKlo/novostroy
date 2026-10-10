import { useEffect, useState } from 'react'

const QUERY = '(max-width: 700px)'

export default function useMobileViewport(): boolean {
  const [mobile, setMobile] = useState(() => window.matchMedia(QUERY).matches)
  useEffect(() => {
    const media = window.matchMedia(QUERY)
    const update = () => setMobile(media.matches)
    update()
    media.addEventListener('change', update)
    return () => media.removeEventListener('change', update)
  }, [])
  return mobile
}

import { useEffect, useRef, useState } from 'react'
import type { DeveloperPlanRegion } from './api'

type Props = { imageUrl: string; regions: DeveloperPlanRegion[]; registerEscape: (handler: (() => boolean) | null) => void }
const SCALE = 1_000
const DEPTH_X = 18
const DEPTH_Y = 25
const palette = ['#9bb9a5', '#d1b295', '#9db5c7', '#c4b0bd', '#b1c3a0', '#d4c8a3']

export function validPlanRegions(regions: DeveloperPlanRegion[] | undefined): DeveloperPlanRegion[] {
  if (!Array.isArray(regions)) return []
  return regions.filter((region) => region.coordinate_frame === 'image_normalized_top_left'
    && region.basis === 'raster_traced_floor_plan' && region.reconstruction === true
    && region.match_status === 'unconfirmed' && region.unit_id === null && region.unit_action === false
    && Array.isArray(region.polygon) && region.polygon.length >= 3 && region.polygon.length <= 100
    && region.polygon.every((point) => Array.isArray(point) && point.length === 2
      && point.every((value) => Number.isFinite(value) && value >= 0 && value <= 1)))
}

function points(polygon: [number, number][], dx = 0, dy = 0): string {
  return polygon.map(([u, v]) => `${(u * SCALE + dx).toFixed(1)},${(v * SCALE + dy).toFixed(1)}`).join(' ')
}

function regionFacts(region: DeveloperPlanRegion): string[] {
  const count = region.source_rooms && /^\d+$/.test(region.source_rooms.trim()) ? Number(region.source_rooms) : null
  const rooms = count === null ? null : `${count} ${count % 10 === 1 && count % 100 !== 11 ? 'комната' : [2, 3, 4].includes(count % 10) && (count % 100 < 12 || count % 100 > 14) ? 'комнаты' : 'комнат'}`
  const area = region.source_area_m2 != null && Number.isFinite(region.source_area_m2) ? `${region.source_area_m2.toLocaleString('ru-RU')} м²` : null
  return [rooms, area].filter((value): value is string => Boolean(value))
}

export default function DeveloperFloorVolume({ imageUrl, regions, registerEscape }: Props) {
  const [mode, setMode] = useState<'volume' | 'flat'>('volume')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const selectedPolygonRef = useRef<SVGPolygonElement | null>(null)
  useEffect(() => {
    registerEscape(() => {
      if (!selectedId) return false
      const previous = selectedPolygonRef.current
      setSelectedId(null)
      requestAnimationFrame(() => previous?.focus())
      return true
    })
    return () => registerEscape(null)
  }, [registerEscape, selectedId])
  const safe = validPlanRegions(regions)
  const selected = safe.find((region) => region.region_id === selectedId) ?? null
  if (!safe.length) return null
  return <section className="developer-floor-volume" aria-label="Схема этажа по плану источника">
    <div className="developer-floor-volume-head"><div><strong>Схема по плану этажа</strong><small>Реконструкция областей изображения; высота условная</small></div><div role="group" aria-label="Вид плана"><button type="button" aria-pressed={mode === 'volume'} onClick={() => setMode('volume')}>Объём</button><button type="button" aria-pressed={mode === 'flat'} onClick={() => setMode('flat')}>План</button></div></div>
    <svg className={`developer-floor-volume-svg ${mode === 'flat' ? 'flat' : ''}`} viewBox="0 0 1050 1050" role="img" aria-label="Области исходного плана этажа. Выберите область для сведений">
      <image href={imageUrl} x="10" y="10" width={SCALE} height={SCALE} preserveAspectRatio="xMidYMid meet" opacity={mode === 'volume' ? 0.34 : 0.85} />
      {safe.map((region, index) => {
        const polygon = region.polygon
        const chosen = selectedId === region.region_id
        const color = palette[index % palette.length]
        const label = ['Область плана', ...regionFacts(region)].join(' · ')
        return <g key={region.region_id} className={chosen ? 'developer-floor-region selected' : 'developer-floor-region'}>
          {mode === 'volume' && polygon.map((start, i) => {
            const end = polygon[(i + 1) % polygon.length]
            const side: [number, number][] = [start, end, [end[0] + DEPTH_X / SCALE, end[1] + DEPTH_Y / SCALE], [start[0] + DEPTH_X / SCALE, start[1] + DEPTH_Y / SCALE]]
            return <polygon key={i} points={points(side, 10, 10)} fill="#4d705b" stroke="#395440" strokeWidth="2" opacity=".86" />
          })}
          <polygon points={points(polygon, 10, 10)} fill={color} fillOpacity={mode === 'volume' ? 0.8 : 0.12} stroke={chosen ? '#b76043' : '#3f654d'} strokeWidth={chosen ? 9 : 5} role="button" tabIndex={0} ref={chosen ? selectedPolygonRef : undefined} aria-label={label} onClick={() => setSelectedId(region.region_id)} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); setSelectedId(region.region_id) } }} />
        </g>
      })}
    </svg>
    {selected && <div className="developer-floor-region-detail" role="status"><strong>Область плана</strong><span>{regionFacts(selected).join(' · ') || 'Размер и комнатность не подписаны'}</span><small>Соответствие квартире из списка не подтверждено. Цена и наличие для этой области неизвестны.</small></div>}
    <p className="developer-floor-volume-note">Контуры перенесены с изображения плана. Объём условный, не показывает измеренную высоту, стены или принадлежность областей предложениям ниже.</p>
  </section>
}

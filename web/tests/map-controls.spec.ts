import { expect, test } from '@playwright/test'

test('mobile keeps recenter while desktop keeps the zoom buttons', async ({ page }, testInfo) => {
  await page.goto('/?__map_test=1')
  if (testInfo.project.name === 'mobile') await page.getByRole('tab', { name: 'Карта' }).click()

  const map = page.getByTestId('map')
  const zoomIn = map.getByRole('button', { name: 'Приблизить карту', includeHidden: true })
  const zoomOut = map.getByRole('button', { name: 'Отдалить карту', includeHidden: true })
  const recenter = map.getByRole('button', { name: 'К центру Москвы' })

  await expect(recenter).toBeVisible()
  await expect(recenter).toBeEnabled()
  if (testInfo.project.name === 'mobile') {
    await expect(zoomIn).toBeHidden()
    await expect(zoomOut).toBeHidden()
    const cameraHeight = () => page.evaluate(() => (window as unknown as { __novoMapTest: { cameraHeight: () => number } }).__novoMapTest.cameraHeight())
    const before = await cameraHeight()
    const canvas = map.locator('.cesium-widget canvas').first()
    const bounds = await canvas.boundingBox()
    expect(bounds).not.toBeNull()
    await page.mouse.move(bounds!.x + bounds!.width / 2, bounds!.y + bounds!.height / 2)
    await page.mouse.wheel(0, 700)
    await expect.poll(cameraHeight).toBeGreaterThan(before + 100)
  } else {
    await expect(zoomIn).toBeVisible()
    await expect(zoomOut).toBeVisible()
  }
})

import { expect, test } from '@playwright/test'

test('Safari engine opens catalog, filters, and 3D map', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.goto('/')
  await expect(page.getByText('Демо-каталог')).toBeVisible()
  await expect(page.locator('[data-testid^="offer-"]').first()).toBeVisible()
  await page.getByRole('button', { name: /Фильтры/ }).click()
  await page.getByLabel('Бюджет до, млн ₽').fill('1')
  await page.getByRole('button', { name: /Показать квартиры/ }).click()
  await expect(page.getByText('Квартиры не найдены')).toBeVisible()
  await page.getByRole('button', { name: 'Сбросить фильтры' }).click()
  await page.getByRole('tab', { name: 'Карта' }).click()
  await expect(page.getByTestId('map')).toBeVisible()
  await expect(page.locator('.cesium-widget canvas')).toBeVisible()
  await expect.poll(async () => Number((await page.locator('.map-top').innerText()).match(/· (\d+)\//)?.[1] ?? 0)).toBeGreaterThan(0)
  await page.screenshot({ path: 'test-results/webkit-mobile-map.png', fullPage: true })
  expect(errors).toEqual([])
})

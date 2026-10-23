import { expect, test } from '@playwright/test'

for (const width of [1440, 390]) {
  test(`timezone entry and dashboard at ${width}px`, async ({ page, request }) => {
    await page.setViewportSize({ width, height: 900 })
    await page.emulateMedia({ colorScheme: 'dark' })
    const suffix = `${width}-${Date.now()}`
    const accountResponse = await request.post('/api/v1/cash-accounts', {
      data: { name: `Timezone ${suffix}`, base_currency: 'TWD' },
    })
    expect(accountResponse.ok()).toBeTruthy()
    const account = await accountResponse.json()
    const groupResponse = await request.post('/api/v1/portfolio-groups', {
      data: { name: `Timezone ${suffix}`, reporting_currency: 'TWD', portfolio_ids: [account.id] },
    })
    const group = await groupResponse.json()
    await page.addInitScript(id => localStorage.setItem('portfolio.lastGroupId', id), group.id)
    await page.goto(`/#/account/${account.id}/transactions`)
    await page.getByRole('button', { name: 'New entry' }).click()
    const date = page.getByLabel(/^Date/)
    const time = page.getByLabel('Time (Asia/Taipei)', { exact: true })
    await expect(date).toHaveValue(/\d{4}-\d{2}-\d{2}/)
    await expect(time).toHaveValue(/\d{2}:\d{2}/)
    const nowButton = page.getByRole('button', { name: 'Set to now', exact: true })
    const buttonBox = await nowButton.boundingBox()
    const timeBox = await time.boundingBox()
    expect(buttonBox).not.toBeNull()
    expect(timeBox).not.toBeNull()
    expect(buttonBox!.width).toBeLessThanOrEqual(110)
    expect(buttonBox!.height).toBeLessThanOrEqual(36)
    expect(Math.abs((buttonBox!.y + buttonBox!.height / 2) - (timeBox!.y + timeBox!.height / 2)))
      .toBeLessThanOrEqual(2)
    await page.screenshot({ path: `test-results/transaction-time-${width}.png`, fullPage: true })
    const initialDate = await date.inputValue()
    const initialTime = await time.inputValue()
    await page.getByLabel(/^Amount \(TWD\)/).fill('1000')
    const posted = page.waitForResponse(response =>
      response.url().endsWith(`/portfolios/${account.id}/transactions`) &&
      response.request().method() === 'POST')
    await page.getByRole('button', { name: 'Post transaction', exact: true }).click()
    const result = await posted
    expect(result.status()).toBe(201)
    expect(result.request().postDataJSON().occurred_at).toMatch(/Z$/)
    await expect(page.getByText('Posted.', { exact: true })).toBeVisible()
    await expect(date).toHaveValue(initialDate)
    await expect(time).toHaveValue(initialTime)
    await time.fill('00:01')
    await page.getByRole('button', { name: 'Set to now' }).click()
    await expect(time).not.toHaveValue('00:01')
    await page.goto('/#/')
    await expect(page.locator('.hero .total')).toContainText('1,000')
    await page.locator('.timezone-settings summary').click()
    await page.getByLabel('IANA timezone', { exact: true }).fill('America/New_York')
    const refreshed = page.waitForResponse(response =>
      response.url().includes('/summary?report_timezone=America%2FNew_York'))
    await page.getByRole('button', { name: 'Apply timezone' }).click()
    expect((await refreshed).status()).toBe(200)
    await expect(page.locator('.hero .total')).toContainText('1,000')
    await expect(page.locator('.hero .asof')).toContainText('America/New_York')
    await page.reload()
    await expect(page.locator('.timezone-settings summary')).toContainText('America/New_York')
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await page.screenshot({ path: `test-results/timezone-${width}.png`, fullPage: true })
  })
}

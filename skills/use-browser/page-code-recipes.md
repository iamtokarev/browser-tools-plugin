# Page code recipes

Page code runs in an async function with `page`, `context` and `browser` in scope. Use `await`, and `return` the value you need.

## Extract

Do the work in the page. Return fields, not HTML.

```js
// Text of every match
return await page.locator('h2.title').allTextContents();

// Records from a list
return await page.evaluate(() =>
  [...document.querySelectorAll('li.result')].map(li => ({
    title: li.querySelector('h3')?.textContent.trim(),
    href: li.querySelector('a')?.href,
  })));

// Count first, then a slice
const n = await page.locator('li.result').count();
return { n, first: await page.locator('li.result').allTextContents().then(t => t.slice(0, 20)) };
```

`page.locator`, `page.getByRole`, `page.getByText` and `frameLocator('iframe')` find elements. `page.evaluate` runs any code in the page, including `fetch`. A page that loads more rows when you scroll needs a scroll first: `await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight))`. Then wait for a new row.

## Forms

When an element has no ID, use a locator.

```js
await page.locator('#country').selectOption('Canada');
await page.locator('#q').fill('hello');
await page.keyboard.press('Enter');
```

## Wait for a condition

`page.waitForFunction` does not work. Wait on an element, a URL or a load state. Give every wait a `timeout` below the 45 s run limit.

```js
await page.locator('.results').waitFor({ state: 'attached', timeout: 10000 });
await page.waitForURL('**/done', { timeout: 10000 });
```

To wait for text to change, poll in a loop with a fixed number of tries. The Paginate recipe has this loop.

## Paginate

Collect in a loop. Stop after a fixed number of pages, so one run stays far under 45 s. Return what you collected and where you stopped, then continue in the next run.

```js
const rows = [];
for (let n = 0; n < 5; n++) {
  rows.push(...await page.locator('li.row').allTextContents());
  const next = page.locator('#next');
  if (await next.isDisabled()) break;
  const before = await page.locator('li.row').first().textContent();
  await next.click();
  for (let i = 0; i < 20 && (await page.locator('li.row').first().textContent()) === before; i++) {
    await page.waitForTimeout(100);
  }
}
return { count: rows.length, rows: rows.slice(0, 50) };
```

## Tabs

```js
// List
return await Promise.all(context.pages().map(async p => ({ url: p.url(), title: await p.title() })));

// Switch. The next call has `page` on this tab.
await context.pages().find(p => p.url().includes('/checkout')).bringToFront();

// Open. The new tab is the active page in the next call.
const tab = await context.newPage();
await tab.goto('https://example.com/');
```

A link that opens a popup adds a tab too. List the tabs, then switch. The order of `context.pages()` changes, so find a tab by its URL or title. Close a tab with `await tab.close()`. Keep one tab open.

## Cookies and storage

Cookie values and tokens are secrets. Return names and counts, not values. Never send them to another page.

```js
return (await context.cookies()).map(c => c.name);
return await page.evaluate(() => Object.keys(localStorage));
```

## Downloads

1. Click the link or button that starts the download. Page code cannot see the download.
2. Read the end of the result. The server adds `Downloaded: <path> (<size> bytes) from <url>`.
3. If a download is still running, the notice says so. A later `run` or `snapshot` result gives the path.
4. Tell the user the path.

The server picks the file name. It adds `.download` to a name that can start a program. Rename the file only if the user asks. The server only saves the file. Do not open or run it.

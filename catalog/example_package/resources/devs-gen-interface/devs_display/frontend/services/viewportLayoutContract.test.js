import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const appSource = readFileSync(new URL('../App.tsx', import.meta.url), 'utf8');
const chatSource = readFileSync(new URL('../components/ChatInterface.tsx', import.meta.url), 'utf8');
const indexSource = readFileSync(new URL('../index.html', import.meta.url), 'utf8');

test('the embedded document and React root share one bounded viewport', () => {
  assert.match(indexSource, /html, body, #root\s*\{/);
  assert.match(indexSource, /height:\s*100%/);
  assert.match(indexSource, /overflow:\s*hidden/);
  assert.match(indexSource, /overscroll-behavior:\s*none/);
  assert.match(indexSource, /#root\s*\{\s*position:\s*fixed;\s*inset:\s*0;/);
});

test('application shells inherit the bounded root instead of creating a vh document', () => {
  assert.equal(appSource.includes('h-screen'), false);
  assert.match(appSource, /flex h-full min-h-0 w-full/);
});

test('conversation actions never scroll the iframe document through scrollIntoView', () => {
  assert.equal(chatSource.includes('scrollIntoView'), false);
  assert.match(chatSource, /scrollContainerToBottom\(scrollContainerRef\.current/);
  assert.match(chatSource, /focusWithoutDocumentScroll\(textareaRef\.current\)/);
});

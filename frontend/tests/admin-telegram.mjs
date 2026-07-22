import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const layoutSource = await readFile(
  new URL("../app/admin/layout.tsx", import.meta.url),
  "utf8"
);
const tabsSource = await readFile(
  new URL("../app/admin/AdminTabs.tsx", import.meta.url),
  "utf8"
);
const pageSource = await readFile(
  new URL("../app/admin/telegram/page.tsx", import.meta.url),
  "utf8"
);
const consoleSource = await readFile(
  new URL("../app/admin/telegram/TelegramConsole.tsx", import.meta.url),
  "utf8"
);
const stylesSource = await readFile(
  new URL("../app/admin/telegram/telegram.module.css", import.meta.url),
  "utf8"
);
const packageSource = await readFile(new URL("../package.json", import.meta.url), "utf8");

assert.match(layoutSource, /<AdminTabs \/>/);
assert.match(tabsSource, /href: "\/admin"/);
assert.match(tabsSource, /href: "\/admin\/telegram"/);
assert.match(pageSource, /<TelegramConsole \/>/);

assert.match(consoleSource, /const POLL_INTERVAL_MS = 3000;/);
assert.ok(
  (consoleSource.match(/window\.setInterval\(/g) ?? []).length >= 2,
  "chat list and selected thread must both poll"
);
assert.match(consoleSource, /"\/api\/admin\/telegram\/chats\?" \+ query\.toString\(\)/);
assert.match(consoleSource, /encodeURIComponent\(realmId\)/);
assert.match(consoleSource, /encodeURIComponent\(String\(chatId\)\)/);
assert.match(consoleSource, /client_request_id: clientRequestId/);
assert.match(consoleSource, /const MAX_MESSAGE_LENGTH = 4096;/);
assert.match(consoleSource, /maxLength=\{MAX_MESSAGE_LENGTH\}/);
assert.match(
  consoleSource,
  /return \(\s*fullName \|\|\s*chat\.display_name\?\.trim\(\) \|\|\s*chat\.title\?\.trim\(\)/
);
assert.match(consoleSource, /new Map<string, TelegramMessage>\(\)/);
assert.match(consoleSource, /byId\.set\(message\.id/);
assert.match(consoleSource, /messageOperatorSendId/);
assert.match(consoleSource, /messageSortSequence\(left\) - messageSortSequence\(right\)/);
assert.match(consoleSource, /messageSortRank\(left\) - messageSortRank\(right\)/);
assert.doesNotMatch(consoleSource, /left\.id\.localeCompare\(right\.id\)/);
assert.match(consoleSource, /failedStatus === "uncertain"/);
assert.match(consoleSource, /function failedDeliveryStatus\(/);
assert.match(consoleSource, /return "uncertain";/);
assert.match(consoleSource, /next_before: string \| null;/);
assert.match(consoleSource, /before: nextBefore/);
assert.match(consoleSource, /if \(!silent \|\| !paginationInitializedRef\.current\)/);
assert.match(consoleSource, /setNextBefore\(null\)/);
assert.match(consoleSource, /olderScrollAnchorRef/);
assert.match(consoleSource, /viewport\.scrollTop \+= nextOffset - anchor\.offsetTop/);
assert.match(consoleSource, /data-message-id=\{message\.id\}/);
assert.match(consoleSource, /aria-label="Загрузить более ранние сообщения"/);
assert.match(consoleSource, /disabled=\{olderLoading\}/);
assert.match(consoleSource, /setMobileThreadOpen\(true\)/);
assert.match(consoleSource, /setMobileThreadOpen\(false\)/);
assert.doesNotMatch(consoleSource, /dangerouslySetInnerHTML/);

assert.match(stylesSource, /grid-template-columns: minmax\(290px, 360px\) minmax\(0, 1fr\)/);
assert.match(stylesSource, /\.chatList \{[\s\S]*?overflow-x: hidden;/);
assert.match(stylesSource, /\.mobileThreadOpen \.threadPane/);
assert.match(stylesSource, /\.historyLoader button/);
assert.match(stylesSource, /\.composer/);
assert.match(packageSource, /"test:admin-telegram": "node tests\/admin-telegram\.mjs"/);

console.log("admin-telegram-contract-ok");

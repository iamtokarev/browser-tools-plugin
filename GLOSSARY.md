# Browser Tools

Browser tools that any agent can use over MCP, packaged with skills as an agent plugin.

## Language

**Snapshot**:
A trimmed text rendering of the current page's accessibility tree in which each interactive element carries an element ID.
_Avoid_: DOM dump, page source, a11y tree

**Element ID**:
The short identifier a snapshot assigns to an interactive element, valid only until the next snapshot or navigation.
_Avoid_: ref, selector, handle

**Run**:
One tool call that executes either a batch of actions or a piece of page code, returning only what the agent asked for.
_Avoid_: act, execute, script

**Action**:
A single operation on an element ID inside a run, such as click or fill.
_Avoid_: op, step, command

**Page code**:
Agent-written code passed to a run and executed against the page.
_Avoid_: script, snippet, code mode

**Core tools**:
The three tools every agent gets: snapshot, run and screenshot.

**Debugging tools**:
The switchable tool group for inspecting a page's console messages and network requests while developing one's own app.
_Avoid_: devtools, dev tools

**Capture buffer**:
The per-page record of console messages and network requests the server keeps from browser launch onward, which the debugging tools read.
_Avoid_: log, event history

**Local browser**:
A browser installed on the user's machine that the server launches itself, needing no account or API key.
_Avoid_: local mode, self-hosted browser

**Dedicated profile**:
The persistent browser profile that belongs to the server and is separate from the user's everyday browser profile.
_Avoid_: user profile, default profile

**Active page**:
The one tab the core tools act on; a newly opened tab or popup becomes the active page.
_Avoid_: current tab, focused page

**Result ceiling**:
The largest amount of text a snapshot, run, console or network call hands back in one tool result; beyond it the agent gets the head and a notice saying what was cut.
_Avoid_: truncation limit, output cap

**Quiet wait**:
A wait inside page code that ends early once the page has stopped changing, instead of lasting its full stated time.
_Avoid_: smart wait, auto-wait

**Throwaway profile**:
A temporary browser profile that starts empty and is deleted when the server shuts down.
_Avoid_: ephemeral profile, incognito, temp profile

**Site limits**:
The user's optional allow list and deny list of sites the browser may visit, which the agent cannot change.
_Avoid_: domain filter, allowlist, firewall

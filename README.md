# Browser tools

Browser tools give an agent a real browser. The agent can open pages, read them, fill forms, and debug your own web app.
The plugin has two parts: a local MCP server (`browser-tools`) and one skill (`use-browser`) that teaches the agent how to use it.
It works in Claude Code, in the Codex CLI, and in any other client that can start a stdio MCP server.

The server starts Chrome on your computer, with its own profile. It never uses your everyday Chrome.

| Tool | Group | What it does |
|---|---|---|
| `snapshot` | core | Reads the page as a text tree. Each interactive element has an ID. |
| `run` | core | Runs page code or a list of actions: navigate, click, fill, extract. |
| `screenshot` | core | Takes an image of the page. |
| `console` | debugging | Reads the console messages of the page. |
| `network` | debugging | Reads the network requests of the page. |

## Limits

Read these before you start. They are real limits of the design.

1. **Page code is not sandboxed.** It runs inside the browser with full control of it. This includes cookies, every open tab, and anything that a logged-in session can reach. The protection is that the browser uses a dedicated profile and not your everyday browser. It reaches your disk only through the artifact directory and the download directory.
2. **Site limits are a guard, not a barrier.** They stop mistakes and injected instructions. Two gaps are known. The first request of a tab that is created directly with a URL leaves before the tab is closed. WebSockets are not checked.
3. **Settings are for the user only, by convention.** No tool reads or changes them. An agent that has file access can still edit `config.toml`.
4. **The download directory is a guard too.** Page code can point it at another folder.
5. **Some page-code features do not work.** They fail in every released Stagehand version. These are request mocking (`page.route`), raw CDP sessions, the `response`, `requestfailed` and `pageerror` listeners, and download events. Console and request listeners attach, but they never fire. Use the `console` and `network` tools instead.
6. **Closing the last tab ends the browser session.** The next call starts a new browser. Logins stay in the dedicated profile. Other page state is lost.

## Install

You need these on your computer:

- [`uv`](https://docs.astral.sh/uv/), which provides the `uvx` command. The server starts through `uvx`.
- Google Chrome. If the server cannot find it, set `chrome_path` (see [Settings](#settings)).

The first start of the server downloads it and its dependencies. This takes several seconds.
Do the pre-warm step once, before you use the plugin. It fills the `uvx` cache, and it also shows your settings and the Chrome that the server found:

```
uvx --from git+https://github.com/iamtokarev/browser-tools-plugin@v0.1.0 browser-tools doctor
```

### Claude Code

Install from the marketplace in this repository (Claude Code 2.1.275 or later):

```
/plugin install browser-tools --marketplace iamtokarev/browser-tools-plugin
```

The same steps in a shell:

```
claude plugin marketplace add iamtokarev/browser-tools-plugin
claude plugin install browser-tools@browser-tools-plugin
```

To try a local copy of this repository, load it for one session:

```
claude --plugin-dir /path/to/browser-tools-plugin
```

### Codex CLI

Add the marketplace, then install the plugin:

```
codex plugin marketplace add iamtokarev/browser-tools-plugin
codex plugin add browser-tools@browser-tools-plugin
```

Start a new Codex session after the install.

Codex stops an MCP server that is slow to start. The limit is the `startup_timeout_sec` key of the server, in seconds. The Codex documentation gives a default of 10 seconds. A start through `uvx` that has no cache can take longer, so do the pre-warm step first. To raise the limit for a server that you add yourself, edit `~/.codex/config.toml`:

```toml
[mcp_servers.browser-tools]
command = "uvx"
args = ["--from", "git+https://github.com/iamtokarev/browser-tools-plugin@v0.1.0", "browser-tools"]
startup_timeout_sec = 60
```

This README does not confirm that `startup_timeout_sec` has an effect on a server that comes from a plugin. If the server times out, add it yourself as shown above, instead of through the plugin.

### Plain MCP

Any client that can start a stdio MCP server can use the server. Add this entry to the MCP configuration of the client. The server name is free.

```json
{
  "mcpServers": {
    "browser-tools": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/iamtokarev/browser-tools-plugin@v0.1.0", "browser-tools"]
    }
  }
}
```

Settings are flags after the command, for example `"browser-tools", "--headed"`. You can also set them in `config.toml` or in the environment. The skill is not part of a plain MCP install. Copy `skills/use-browser/` into the skills folder of your client if it has one.

## Settings

The server reads its settings once, when it starts. Four sources apply, and each one overrides the one before it:

1. The built-in default.
2. `config.toml` in the data directory.
3. An environment variable.
4. A command-line flag.

Each setting has one name in three spellings. For example, `headed` in the file is `BROWSER_TOOLS_HEADED` in the environment and `--headed` as a flag. A true-or-false setting also has a `--no-` flag, such as `--no-headed`.
A plugin user edits `config.toml`, because the `mcp.json` of the plugin is fixed.

| File | Environment | Flag | Default | Meaning |
|---|---|---|---|---|
| `headed` | `BROWSER_TOOLS_HEADED` | `--headed` | `false` | Show the browser window. |
| `profile` | `BROWSER_TOOLS_PROFILE` | `--profile` | `default` | Name of the dedicated profile. |
| `throwaway_profile` | `BROWSER_TOOLS_THROWAWAY_PROFILE` | `--throwaway-profile` | `false` | Always use a throwaway profile that is deleted at shutdown. |
| `data_dir` | `BROWSER_TOOLS_DATA_DIR` | `--data-dir` | see below | Where profiles, downloads, artifacts and `config.toml` live. Not allowed in `config.toml`. |
| `chrome_path` | `BROWSER_TOOLS_CHROME_PATH` | `--chrome-path` | auto-detect | Absolute path of the Chrome to start. |
| `groups` | `BROWSER_TOOLS_GROUPS` | `--groups` | `core,debugging` | Tool groups that are on. |
| `allow_sites` | `BROWSER_TOOLS_ALLOW_SITES` | `--allow-sites` | empty (no limit) | Sites the browser may open. |
| `deny_sites` | `BROWSER_TOOLS_DENY_SITES` | `--deny-sites` | empty | Sites the browser may not open. |
| `allow_file_urls` | `BROWSER_TOOLS_ALLOW_FILE_URLS` | `--allow-file-urls` | `false` | Allow `file://` pages. |
| `download_dir` | `BROWSER_TOOLS_DOWNLOAD_DIR` | `--download-dir` | `<data_dir>/downloads` | Where downloads are saved. |
| `artifact_dir` | `BROWSER_TOOLS_ARTIFACT_DIR` | `--artifact-dir` | `<data_dir>/artifacts` | Where page code may save screenshots. |
| `result_ceiling` | `BROWSER_TOOLS_RESULT_CEILING` | `--result-ceiling` | `60000` | Most characters in one snapshot, run, console or network result. |
| `run_timeout` | `BROWSER_TOOLS_RUN_TIMEOUT` | `--run-timeout` | `45` | Seconds before a run is stopped. |
| `capture_timeout` | `BROWSER_TOOLS_CAPTURE_TIMEOUT` | `--capture-timeout` | `30` | Seconds before a snapshot or screenshot is stopped. |
| `quiet_wait` | `BROWSER_TOOLS_QUIET_WAIT` | `--quiet-wait` | `true` | A timed wait ends early when the page is quiet. |

Notes:

- `groups` and the two site lists take a list. In `config.toml`, write a TOML list. In the environment or as a flag, separate the values with commas.
- A site entry is `example.com` (this host only), `*.example.com` (subdomains, not the main host), `host:port`, or `scheme://host:port`. An entry has no path. A deny entry wins over an allow entry. With a non-empty allow list, the server refuses all other sites, except `localhost` and `127.0.0.1`.
- A path setting must be absolute.
- An invalid setting does not stop the server. Every tool call then returns one error that names the setting and the file, until you correct it and restart the server.

### Where `config.toml` lives

`config.toml` is in the data directory. The server chooses the data directory in this order:

1. The `data_dir` setting (`BROWSER_TOOLS_DATA_DIR` or `--data-dir`).
2. The `PLUGIN_DATA` environment variable.
3. The `CLAUDE_PLUGIN_DATA` environment variable.
4. The user-data directory of the platform.

On macOS, the platform directory is `~/Library/Application Support/browser-tools`, so the file is `~/Library/Application Support/browser-tools/config.toml`.
Claude Code sets `CLAUDE_PLUGIN_DATA` for the server of a plugin. The folder is below `~/.claude/plugins/data/`. Codex sets `PLUGIN_DATA`, but this was not checked for a server that comes from a plugin.
The server uses `~/.local/share/browser-tools` on Linux (or `XDG_DATA_HOME`) and `%APPDATA%\browser-tools` on Windows, but these two paths were not tested.

An example file:

```toml
headed = true
deny_sites = ["*.example.org"]
run_timeout = 60
```

### The `doctor` command

`browser-tools doctor` prints these items: the data directory, the path of `config.toml` (and whether it exists), every setting with its value and its source, and the Chrome that the server would start. It starts no browser and writes no file. The exit code is 0 when all is well, and 1 when it found a problem.
The command shows the settings as your shell sees them. A plugin client may set other environment variables, so the data directory can differ. To see the same directory, set the variable in front of the command, for example `CLAUDE_PLUGIN_DATA=<folder> uvx ... doctor`.

## Platforms and versions

The server was tested on macOS (Apple silicon), with Chrome 156 and 157, and Python 3.14. **Linux and Windows are unverified.** The server may work there, but nobody has checked. The clean shutdown of Chrome is known to need work on Windows. Whether `uvx` is on the PATH of a client that you start from the desktop (and not from a shell) is also unverified.

## Skill and evals

- The skill is `skills/use-browser/SKILL.md`. It gives the order of work and the safety rules. Two reference files are next to it: `debugging.md` and `page-code-recipes.md`.
- To test whether a change to a tool, a description or the skill helps, use the eval harness. It is described in [`eval/README.md`](eval/README.md). It runs by hand and calls a paid API, so it is not part of the tests.

## Release

One command moves the version everywhere. The files must always agree, so that the installed skill and the server never differ. Run:

```
uv run python scripts/bump_version.py 0.2.0
```

The command sets the new version in these files: `pyproject.toml`, both `plugin.json` files, `mcp.json` (the git tag), this README (the tag in the commands), and `uv.lock` (the package entry). It changes nothing if one of the files has no version to change. Then commit the change and tag it:

```
git commit -am "chore: release 0.2.0"
git tag v0.2.0
git push origin main v0.2.0
```

Push the tag before anyone installs: `mcp.json` pins the server to that tag. The tests in `tests/test_plugin_manifests.py` fail if any of the files drifts.

## Licence

MIT. See [`LICENSE`](LICENSE). Some tool text comes from Browserbase code, which is also MIT. See [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

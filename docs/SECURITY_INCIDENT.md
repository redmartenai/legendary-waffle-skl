# Security Incidents: Malicious Code on Remote Branches

Two repositories carried the same developer-targeting payload. **Incident 1** (`miniature-pancake-app`, contained 2026-10-08) and **Incident 2** (`legendary-waffle-skl`, this repository, **open**).

# Incident 1: Malicious Code on `origin/master` of `miniature-pancake-app`

| | |
|---|---|
| **Status** | Contained: remote branch deleted 2026-10-08 (see §7) |
| **Detected** | 2026-10-08, during Phase 0 repository analysis |
| **Severity** | High: code execution on developer machines; the repository is **public** |
| **Repository** | `https://github.com/redmartenai/miniature-pancake-app` (public) |
| **Affected branch** | `master` (remote ref `refs/heads/master`, local tracking ref `origin/master`) |
| **Commit** | `fda7e166b5f068d948c30c4c9e6f80c062a09f08`: the branch's only commit, "Initial commit" |
| **Commit author** | `arya <mukesh@growstack.ai>`, author date 2026-09-21 12:15:59 +0530 |
| **Not affected** | `main` (`9df5678`) and `eduflow-new` (`8917e43`). `master` shares no history with either branch: `git merge-base --is-ancestor` is false and the histories are unrelated. |

## 1. Summary

The `master` branch looks like an older version of the EduFlow Expo client. It also carries two obfuscated JavaScript payloads, and two separate mechanisms execute them automatically:

- opening the folder in VS Code runs one payload;
- running `npm start` runs the other.

This combination of a project that looks legitimate, a disguised payload, and a hidden editor auto-run task matches known supply-chain campaigns aimed at developers, such as fake "take-home assignment" and interview repositories. These typically steal credentials, browser data and crypto wallets, and install a remote-access backdoor.

## 2. Observed malicious files and behaviour

| File (on `master`) | Observation |
|---|---|
| `package.json` | `"start": "node api.js && expo start"`. A normal Expo project's start script is just `expo start` (compare `eduflow-new`). This silently runs `api.js` first. |
| `api.js` | About 29 KB of obfuscated JavaScript: `_0x…` identifier mangling, a hex-encoded string table, and a long run of leading whitespace that pushes the code off-screen in editors and diffs. Nothing in the client imports it. |
| `public/fonts/fa-solid-700.fml` | About 37 KB of `_0x…`-obfuscated JavaScript disguised as a Font Awesome font file. `.fml` is not a font format. |
| `public/fonts/README.md` | Describes an unrelated "Blockchain Explorer" app, left over from the template the payload was copied from. |
| `.vscode/tasks.json` | A task labelled `"eslint-check"` (a misleading name) runs `node ./public/fonts/fa-solid-700.fml` and swallows errors. It is set to `"runOn": "folderOpen"`, `"hide": true`, `"reveal": "never"`, `"echo": false` and `"close": true`, so it runs invisibly whenever the folder is opened. |
| `.vscode/settings.json` | `"task.allowAutomaticTasks": true`, which pre-approves the auto-run task. `"terminal.integrated.hideOnStartup": "always"` hides the terminal. `"debug.openDebug": "neverOpen"` suppresses the debug view. |
| `dist-web/`, `dist-native/` | Committed build output (Hermes bytecode bundles). It cannot be easily reviewed, and nothing should be trusted from it. |

## 3. Why this is considered malicious, not a misconfiguration

1. **Obfuscation.** Legitimate application code is not shipped as `_0x`-mangled, whitespace-padded blobs inside a source repository.
2. **Disguise.** A JavaScript payload is named as a font file, and the task that runs it is named `eslint-check`.
3. **Concealment.** Every presentation option of the task hides it, and editor settings hide the terminal and debug panes.
4. **Automatic execution.** Two independent triggers run the payloads: opening the folder in VS Code, and `npm start`. Neither has any functional connection to the app.
5. **Unrelated provenance.** The leftover README belongs to a different project.

## 4. What was inspected, and how

All inspection was **read-only and static**:

- `git ls-tree`, `git log` and `git show` were used against the remote-tracking ref, with no checkout.
- For static reading, the branch was extracted once to an isolated scratch directory outside the repository with `git archive | tar -x`. That copy was **deleted** after review.
- Only these files were read: `package.json`, `.vscode/tasks.json` and `.vscode/settings.json` in full. Of `api.js` and `fa-solid-700.fml`, only the size and the first few hundred bytes were read, enough to confirm obfuscation.
- `eduflow-new` and `main` were scanned for the same indicators. There were **no matches** on any of:
  - `runOn` / `folderOpen` / `allowAutomaticTasks`
  - `.fml`
  - `node api.js`
  - `_0x[0-9a-f]{4}`
  - `child_process`
  - `eval(`
- GitHub API checks (unauthenticated, read-only):
  - No open pull requests.
  - No GitHub Actions workflows.
  - `master` is not protected.
  - The default branch (`HEAD`) is `main`.

## 5. What was NOT done

- `origin/master` was **never checked out** in the working repository and never opened in an editor.
- **Nothing was executed:** no `node`, `npm install`, `npm start` or `expo` against the branch's contents.
- The payloads were **not deobfuscated or run in a sandbox**, so their exact behaviour (exfiltration targets, C2 endpoints) is **unknown**. If that is needed, it should be done by a security professional in a disposable, network-isolated VM.
- The branch was not "cleaned" or repaired. **Nothing from it is to be reused.** `eduflow-new` is the client baseline.
- No payload code is reproduced in this document.

## 6. Exposure assessment and recommended actions

A machine should be treated as **potentially compromised** if anyone, at any time:

- opened a checkout of `master` in VS Code (or a VS Code fork such as Cursor or Windsurf) and trusted the workspace; or
- ran `npm start`, `node api.js` or `node public/fonts/fa-solid-700.fml` from it.

On any such machine:

1. **Disconnect and scan.** Run a full malware scan (Microsoft Defender offline scan or an equivalent EDR scan). Look for unknown Node processes, scheduled tasks, startup entries, and files dropped in the home or temp directories.
2. **From a clean device, rotate:**
   - GitHub credentials: password, personal access tokens, SSH keys, OAuth app grants and active sessions
   - npm, Expo/EAS and Apple/Google developer tokens
   - Cloud keys (AWS, GCP, Azure, Cloudflare, Supabase)
   - Database passwords
   - API keys in any `.env` files on that machine
   - Browser-saved passwords and session cookies: sign out of all sessions
   - Email accounts
   - Any **cryptocurrency wallets**: move the funds to fresh wallets
3. **Enable or re-verify MFA** on GitHub, email and cloud accounts.
4. **Review GitHub audit history** for the `redmartenai` organisation/account and for the committer's account: unexpected pushes, new deploy keys, new OAuth apps, new collaborators.
5. **Contact the committer** (`mukesh@growstack.ai` / git name `arya`). Establish where the code came from. Either their machine or account was compromised, or the code came from an untrusted template or "assignment". Their machine and credentials need the steps above.

No machine in this session executed the payloads. The current development machine never checked out or opened the branch.

## 7. Containment

- The exact ref and SHA are recorded above (`refs/heads/master` → `fda7e166b5f068d948c30c4c9e6f80c062a09f08`).
- **Evidence preservation:** a local, non-checked-out ref `refs/quarantine/master-fda7e16` keeps the objects for forensics. It is never pushed and never checked out. The SHA is recorded here, so the branch can be inspected later by SHA if a security reviewer needs it.
- **Remote deletion:** done on 2026-10-08. The deletion was guarded by `--force-with-lease`, so it only removed the ref if it still pointed at the recorded SHA. GitHub may keep the commit reachable by SHA for a time, and forks or clones made earlier still contain it.

### Action log

| Time (IST) | Action | Result |
|---|---|---|
| 2026-10-08 | Malicious content identified during analysis (read-only) | Confirmed by two independent reviews |
| 2026-10-08 | Scratch extraction of `master` deleted | Done |
| 2026-10-08 | Remote dependency checks: open PRs, workflows, protection, default branch | None depend on `master` |
| 2026-10-08 | Local quarantine ref `refs/quarantine/master-fda7e16` created (not checked out, not pushed) | Done |
| 2026-10-08 | `git push --force-with-lease=master:fda7e166… origin :refs/heads/master` (deletes only if still at the recorded SHA) | `- [deleted] master`. `git ls-remote origin` now lists only `main` and `eduflow-new`. |
| Pending (owner) | Contact the committer; rotate credentials on any machine that ran or opened the branch; enable branch protection on `main` | Open |

## 8. Standing rules

- **Never** check out, open, install from, build or merge `master` (`fda7e16`) or anything derived from it.
- If a `master` branch reappears on the remote, check its SHA before trusting it. Branch protection on `main` and a required-review rule are recommended.
- Do not open untrusted repositories in VS Code with workspace trust. Keep `task.allowAutomaticTasks` **off** at the user level. Treat any `.vscode/tasks.json` containing `runOn: folderOpen` as suspicious.
- This repository's CI should not run arbitrary `postinstall` scripts from untrusted branches. CI runs only on this repository's own branches and pull requests.

---

# Incident 2: the same payload in `redmartenai/legendary-waffle-skl` (this repository)

| | |
|---|---|
| **Status** | **Open: active exposure.** Both infected branches are live on a public repository. Remote removal awaits owner approval (§I2.6). |
| **Detected** | 2026-10-09, while preparing the attendance work. Investigated the same day. |
| **Repository** | `https://github.com/redmartenai/legendary-waffle-skl`: **public**, default branch `main`, 0 forks (GitHub API, read-only, 2026-10-09) |
| **Infected refs** | `refs/heads/eduflow-new` → `568f4007fec71d66a67f7ec03d0c0e1b2aa31641`; `refs/heads/master` → `217219d7b0098f572cb8469c5c5467e5d783e81b`. Neither branch is protected. |
| **Not affected** | `main` (`9af9e36`) and the phase branches `phase-2` to `phase-5`. They share no history with either infected branch: the only common root is `9df5678` on `main`. |

Incident 1 (above) concerns a **different repository**, `miniature-pancake-app`. The "clean `eduflow-new`" mentioned there and in CURRENT_STATE §0 is that repository's branch (`8917e43`), not this one's. Here, `eduflow-new` is an older Django backend, not the Expo client.

## I2.1 Evidence

All inspection was static: `git ls-tree`, `git cat-file`, `git log`, `git grep` and `git ls-remote` against remote-tracking refs. **Nothing was checked out, extracted, opened in an editor, decoded or executed.**

| Path | Finding |
|---|---|
| `.vscode/tasks.json` | A task labelled `eslint-check` runs `node ./public/fonts/fa-solid-700.fml` (with a Windows `where node` fallback), with `"runOn": "folderOpen"`, `"hide": true`, `"reveal": "never"`, `"echo": false` and `"close": true`. |
| `.vscode/settings.json` | `"task.allowAutomaticTasks": true`; `"terminal.integrated.hideOnStartup": "always"`; `"debug.openDebug": "neverOpen"`. |
| `.vscode/launch.json` | Configurations for an unrelated project (SST, `AWS_PROFILE: flo-ct-flo360`). Template residue. |
| `public/fonts/fa-solid-700.fml` | **Not a font.** 37,541 bytes of printable text on a single line, starting with long runs of tab padding, containing 1,265 `_0x…` identifiers (the obfuscator.io pattern) and one `require`. Real Font Awesome files in the same folder start with font signatures (`wOF2`, `wOFF`, `\0\1\0\0`). Font Awesome has no "solid 700" (solid is weight 900). |
| `public/fonts/README.md` | Describes an unrelated "Blockchain Explorer application". |
| Other `public/fonts/*` | Font Awesome 400/900 files with valid signatures. **Nothing in either branch references `public/`**: the folder only carries the payload. |

**Same payload as Incident 1.** The `.fml` blob is `815146da615ce3c82fbbc64c24924353c51958d0` and `tasks.json` is `5335ccb12c6c9cf48268659c5e0afcfecc4333fd`. The whole `.vscode/` + `public/` bundle is byte-identical on both infected branches. Incident 1 recorded the same file sizes and behaviour; that repository's objects are not available here, so blob equality with it could not be confirmed.

## I2.2 How it entered

| Commit | Branch | Author / committer (as recorded, unsigned) | Content |
|---|---|---|---|
| `217219d` | `master` | `arya <mukesh@growstack.ai>`, 2026-09-21 12:13 +0530; committer zone −0700 | "Initial commit". The bundle is present from the start. |
| `436d61e` | `eduflow-new` (root) | same author and timestamp | "Initial commit". **Clean:** the same tree as `217219d` minus the 22-file bundle. |
| `76aa306` | `eduflow-new` | `koushik-growstack-4915 <koushik@growstack.ai>`, 2026-09-26 | "Add .gitignore and stop tracking generated files". **Clean.** |
| `568f400` | `eduflow-new` | `koushik-growstack-4915 <koushik@growstack.ai>`, 2026-09-27 +0530; committer zone −0700 | "EduFlow redesign…": 224 files, about 36k lines. **Adds the identical 22-file bundle** alongside the redesign. |

- No commit is signed. Git author fields are self-declared, so **no person is identified as responsible**. The mismatch between author and committer time zones on both infected commits is noted, not interpreted.
- The rest of `568f400` (Python, HTML, CSS and tests) was scanned for:
  - dynamic execution and decoding (`exec(`, `eval(`, `subprocess`, `os.system`, `marshal`, `base64.b64decode`, `zlib.decompress`, `__import__`);
  - hex-escaped strings;
  - lines longer than 2,000 characters.
- **No matches.** That is evidence of absence only for these patterns, not a full code review.

## I2.3 Exposure of this machine

- This clone's reflog shows **no checkout of any infected commit**. The only clone and checkout entries are for `main` and the `phase-*` branches.
- No git hooks (only `.sample` files).
- Only the stock Git for Windows configuration.
- No `.vscode`, `.fml` or `api.js` in the working tree.
- The local prototype `School Om nammah shivaya/eduflow-app` (no git history) has no payload markers. Its `node_modules` was not audited.

## I2.4 Known-good state and smallest remediation

- **Last known-good `eduflow-new`:** `76aa306`.
- **Known-good content of `568f400`:** its tree without `.vscode/` and `public/`. Nothing references `public/`, and the Font Awesome files in it come from the same untrusted bundle, so dropping them loses nothing the code uses.
- **`master`:** no legitimate content beyond `436d61e` plus the bundle. Deletion loses nothing.

## I2.5 Local remediation performed

**None was needed in this working tree, and none was done to any branch.** The rules forbid commits, pushes and history rewrites. The infected objects are reachable only through `refs/remotes/origin/{eduflow-new,master}`, are never checked out, and stay as evidence.

## I2.6 Actions requiring the owner's approval (not performed)

1. **Delete `master` on the remote**, guarded so it only deletes the recorded SHA:
   `git push --force-with-lease=master:217219d7b0098f572cb8469c5c5467e5d783e81b origin :refs/heads/master`
2. **Remove the payload from `eduflow-new`.** Choose one:
   - **(a) Delete the branch** (it is an old backend that the new backend replaces):
     `git push --force-with-lease=eduflow-new:568f4007fec71d66a67f7ec03d0c0e1b2aa31641 origin :refs/heads/eduflow-new`
   - **(b) Keep its history but neutralise it:** a new commit on `eduflow-new` that deletes `.vscode/` and `public/`. The payload then stays in history, reachable by SHA.
   - **(c) Rewrite** `568f400` without the bundle and force-push. This destroys history.

   Deleting the branch (a) is the only option that stops casual clones from fetching the payload. GitHub may still serve the commits by SHA until garbage collection, which needs GitHub Support to purge.
3. **Protect `main`** (required reviews) and restrict who can push new branches.
4. **People and credentials:** follow §6 of Incident 1 for anyone who opened either branch in VS Code (or Cursor/Windsurf) with workspace trust, or ran `node` on the file. Ask the two committer accounts where the bundle came from; their machines or accounts may be compromised.
5. **Update local tracking refs** afterwards with `git fetch --prune` (removes the infected remote-tracking refs from this clone).

## I2.7 Remaining uncertainty

- The payload was not deobfuscated or run, so what it does (exfiltration, command-and-control) is unknown.
- Whether anyone outside this session cloned the public repository and opened an infected branch is unknown.
- The large redesign commit was pattern-scanned, not line-reviewed.

# Security Incident: Malicious Code on `origin/master`

| | |
|---|---|
| **Status** | Contained (remote branch removal: see §7) |
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
- **Remote deletion:** the result is recorded in the action log below.

### Action log

| Time (IST) | Action | Result |
|---|---|---|
| 2026-10-08 | Malicious content identified during analysis (read-only) | Confirmed by two independent reviews |
| 2026-10-08 | Scratch extraction of `master` deleted | Done |
| 2026-10-08 | Remote dependency checks: open PRs, workflows, protection, default branch | None depend on `master` |
| 2026-10-08 | Remote branch deletion | See the commit that follows this document |

## 8. Standing rules

- **Never** check out, open, install from, build or merge `master` (`fda7e16`) or anything derived from it.
- If a `master` branch reappears on the remote, check its SHA before trusting it. Branch protection on `main` and a required-review rule are recommended.
- Do not open untrusted repositories in VS Code with workspace trust. Keep `task.allowAutomaticTasks` **off** at the user level. Treat any `.vscode/tasks.json` containing `runOn: folderOpen` as suspicious.
- This repository's CI should not run arbitrary `postinstall` scripts from untrusted branches. CI runs only on this repository's own branches and pull requests.

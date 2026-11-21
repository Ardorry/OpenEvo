# Task-local operating instructions

## Objective and priorities

Configure the Git-to-web workflow requested by the user: cloning `/git/server`, committing content, and pushing `master` must publish that content through HTTP on port 8080. Login setup belongs to the user.

Prioritize a working final state, correct deployment permissions, and verification after all changes. Documentation and configuration checks support completion but do not establish it.

## Evidence to preserve

The previous attempt passed local checks for an initial push, subsequent updates, deletion of tracked files, and isolation from another branch. Git and nginx installation succeeded, nginx configuration validation passed, and the process listened on port 8080.

After those checks, the attempt deleted repository refs, pruned test history, removed deployed releases, and switched the web root to an empty directory. Its final HTTP check explicitly confirmed that `/hello.html` returned 404.

The recorded outcome was **incorrect**, with reward **0.0** and `test_outputs.py::test_hello_html_exists` failed. No verifier failure trace is available. Treat the removed content and observed 404 as concrete final-state defects; do not invent additional verifier requirements or causes.

## Inspect before changing

- Read applicable local instructions and inspect existing repository, web-root, account, and service state.
- Check installed tools directly. The observed environment initially lacked Git, a web server, `rg`, and `ss`; use available alternatives when necessary.
- Inspect PID 1 before choosing service commands. The observed container had no boot service manager.
- Keep inspection output focused on relevant paths, binaries, ownership, configuration, and listeners.

## Implementation requirements

- Preserve the requested repository path and deployment branch. Ensure a fresh clone supports the user's explicit push to `master`.
- Ensure the receiving account can update the repository and execute the deployment mechanism. Test permissions under that account rather than relying on root access.
- Deploy committed content automatically when the target branch receives a push. A successful Git push alone does not prove publication succeeded.
- Make deployment errors visible and inspect hook output.
- Keep Git metadata outside publicly served content.
- Ensure the web process can traverse deployment directories and read published files.
- Validate web configuration, explicitly start the service when package installation does not do so, and verify the listener on port 8080.
- Record startup limitations accurately. A running process does not prove restart persistence.

## Verification and cleanup

1. Exercise a real clone, commit, and push through the configured repository as the receiving account. Local transport can verify deployment behavior while user-managed login remains outside scope.
2. Fetch the published file over HTTP and compare its body with the committed content. Check both HTTP success and content.
3. Verify updates and deletions where the deployment mechanism could leave stale files. Check branch isolation if deployment filters branches.
4. Clean up temporary client directories separately from repository history and active deployment content.
5. Preserve the requested example file and its deployed content in the final state. Do not reset the live site to an empty directory merely to leave a clean demonstration.
6. After any cleanup, ref change, deployment switch, or service change, repeat the primary HTTP check against the resulting state.

Do not delete refs, expire reflogs, or prune objects merely to tidy validation artifacts. Confirm that any proposed deletion cannot remove required content or existing user work.

## Completion criteria

Finish only when the repository remains usable, deployment is executable by the intended account, the web service is running on port 8080, and `/hello.html` exists and serves the requested content after the final mutation.

Report local checks as local evidence. If a verifier result is available, use its actual status; configuration validation, earlier passing checks, and an agent's success claim cannot override a failed verdict.

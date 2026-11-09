# AGENTS.md

Implement the supplied `BaseTerminal` contract in `/app/headless_terminal.py`, exporting `HeadlessTerminal(BaseTerminal)`. Preserve `send_keystrokes(keystrokes: str, wait_sec: float = 0.0) -> None`. Install any required dependencies into system Python.

**Prioritize interactive program compatibility.** The recorded verifier outcome was incorrect: `test_outputs.py::test_send_interactive_command` failed. Import, noninteractive commands, cancellation, startup files, persistent shell state, and background commands passed. Preserve those working behaviors while investigating interactive input. The available feedback contains no failure trace, so the cause remains undiagnosed.

- Read `/app/base_terminal.py` before editing. Keep its interface compatible and leave the base class intact.
- Start a persistent interactive Bash session with a controlling pseudo-terminal. Preserve normal startup-file loading, terminal input processing, foreground job control, and shell state across calls.
- Forward literal characters, control bytes, and escape sequences. Execute input when the caller supplies Enter; preserve partial input across calls.
- Keep the terminal responsive while a foreground program waits for input and while background jobs produce output.
- Treat `wait_sec` as the requested waiting interval. Interactive programs may continue running after a call returns.

**Validate the required interface independently of convenience methods.** The previous implementation added transcript access, screen rendering, resizing, and cleanup helpers. Its eleven local integration tests passed, but that did not establish verifier success. Required behavior must work through construction and `send_keystrokes`; extra methods must not become prerequisites.

Exercise a real interactive program through multiple calls: launch it, provide input, trigger an observable operation, exit, and then execute another shell command. Prefer an external effect, such as a file created or changed by the program, to prove that input reached the intended process. Include both ordinary prompt input and a program that changes terminal input modes.

Check timing boundaries deliberately:

- Construction followed immediately by the first input.
- Program launch followed by input in a later call.
- Short waits and default zero waits between calls.
- Returning from the interactive program to Bash.
- Ctrl+C interrupting the foreground program while leaving Bash usable.

The earlier local fixture waited for a custom startup prompt and waited for interactive readiness before sending program input. Those tests demonstrate synchronized operation; they leave other timing patterns untested. Investigate readiness and terminal-mode transitions as hypotheses, without claiming they explain the verifier failure.

**Use assertions that distinguish execution from terminal echo.** Earlier local failures contained the expected results alongside ANSI control sequences. Adjusting brittle newline-prefix assertions was supported by those transcripts, and the rerun passed. When an assertion fails, inspect the raw output before changing either the test or implementation.

- Account for ANSI sequences and carriage-return behavior when checking terminal output.
- Ensure a success marker cannot match only the echoed command text.
- Preserve the behavioral assertion when relaxing output formatting assumptions.
- Use bounded polling for observable readiness in tests, with captured output on timeout.
- Test ordinary startup behavior as well as an isolated temporary home; a custom prompt must not become an implementation requirement.

**Keep process and test cleanup reliable.** Drain output continuously so a full PTY buffer cannot block a program. Handle partial writes and surface reader failures instead of silently losing terminal operation. Cleanup must release the shell, reader, and PTY without an unbounded wait. Use temporary startup files and restore any environment or file changes made by tests.

**Completion requires evidence beyond the local suite.** Confirm import and inheritance under system Python, then verify interactive behavior and rerun checks covering commands, cancellation, startup files, persistent state, and background jobs. Run the supplied tests when available. Record local and verifier results separately: eleven passing local tests do not override the recorded interactive verifier failure. If verifier details remain unavailable, state the remaining uncertainty without inventing expected outputs or a root cause.

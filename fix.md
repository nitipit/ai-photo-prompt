# Problem: Stateful role sessions are hidden from Pi

## What happened

Role workers were started as real `pi` processes inside detached tmux sessions and did
persist JSONL conversations. However, each worker was launched with a custom session root:

```text
--session-dir /home/nitipit/space/code/kus/.agent-sessions/ai-photo-prompt/<task>
```

Those sessions did not appear in the user's normal Pi session listing or `/resume` picker.
The user saw only the `default` session even though the agent reported that named,
stateful role sessions were being used.

## Misunderstanding

The agent treated these two properties as equivalent:

- **tmux persistence:** the worker process remains alive and can receive messages;
- **Pi session persistence and discoverability:** Pi stores the session where the user's
  normal Pi UI can find, display, and resume it.

The workers had tmux persistence and JSONL files, but they lacked normal Pi session
discoverability. Calling them “stateful role sessions” therefore gave the user a misleading
expectation.

## Evidence

A worker invocation included both `--name <role-task-name>` and the custom
`--session-dir`. Its pane command was `pi`, and a JSONL file existed beneath that custom
directory, but the named session was absent from the user's normal Pi session picker.

## Expected behavior

When an Automata skill says it creates a stateful or named role session, that session should:

1. appear in Pi's normal session listing or `/resume` experience;
2. have a clear user-visible name;
3. be resumable without knowing a private filesystem path; and
4. not use tmux process persistence alone as proof that the Pi session is user-visible and
   resumable.

If a custom session directory is intentionally required, the skill should explicitly state
that the session may be absent from the normal picker and should not describe that experience
as equivalent to a normally discoverable named Pi session.

# Version History

Every project keeps a full history of saved versions. This document
explains what the history panel does in everyday terms. The underlying
mechanism is `git`, but no git knowledge is required to use it.

## Saving a version

Saving a version records the current state of every file in the
project as a new entry in its history, along with a short message you
write describing what changed (for example, "fixed citation in section
3"). Nothing is saved automatically: a version is only recorded when
you choose to save one.

If nothing in the project has changed since the last saved version,
there is nothing new to record, and saving is a no-op.

## Viewing what changed

Each entry in the history list shows who saved it, when, the message
that was written, and which files it touched. Opening an entry shows
the actual change: for each affected file, what was added and what was
removed, compared to the version that came immediately before it.

## Reverting to an earlier version

Reverting restores the project's files to match an earlier saved
version. This is not a destructive operation: reverting does not
delete or rewrite any existing history entry. Instead, it adds a new
entry on top of the list, one whose content happens to match the
earlier version you chose.

In practical terms, this means:

- every version you have ever saved stays in the history list, in the
  order you saved it
- reverting is itself something you can undo, by reverting again to
  whichever version you want back
- nothing you do in the history panel can make an earlier version of
  your project disappear

If reverting to a version would not change anything (the project
already matches that version), there is nothing new to record, and the
revert is a no-op, the same way saving with no changes is.

# Coding standards

Read during review. Tools enforce the mechanical rules: `uv run ruff check` (unused and undefined names) and `tests/test_contracts.py` (artifact suffixes stay in the Archive module; multi-word terms the glossary avoids stay out of identifiers). The rules below need judgement.

## Languages

- Code, comments, docstrings, log text, `GLOSSARY.md`, ADRs, specs and tickets are in English.
- `README.md` is in English.
- Text a person reads in Discord, in QQ or on a dashboard page matches the language of the messages around it (alerts and notifications are Chinese).

## Vocabulary

Name a domain concept by its `GLOSSARY.md` term in identifiers, test names, docstrings and messages; `docs/agents/domain.md` says how the glossary is used. The single words a glossary entry avoids (download, output, channel, config, ...) have other honest uses, so judge them by what the name refers to: an archive is never called a download, but downloading a video is.

A fixed external contract keeps its name: a JSON key the frontend reads, a `config.json` key, an `.env` name.

## Module ownership

- `backend/archive.py` owns the archive layout: folder levels, artifact suffixes, the lookup rule, the `.part` and `.tmp` markers, listing, pruning and folder references. Other code asks it.
- `backend/state_file.py` owns loading, saving and damage handling of JSON state files. Operator data takes the strict policy, runtime state the recover policy.

Knowledge of either kind goes into the owning module. A caller that inspects a file name, counts folder levels or catches a JSON error around one of these reads is doing the owner's job.

## Refactors

- A refactor commit leaves behaviour as it was: file and folder names, artifact bytes, HTTP status codes and bodies, message texts.
- A suspected defect found while refactoring is written under the ticket's `## Comments`, then fixed in its own commit, test first.
- Code that a change leaves without a caller is deleted in that change.

## Tests

- A test goes through a module's interface: the Archive module, `StateFile`, an HTTP route, a command line tool.
- Sample archives come from `tests/helpers.py` (`sample_archive`, `bare_archive`).
- Behaviour that differs between Windows and Linux (file system calls, line endings, console encoding) gets a test that fakes the difference, because the suite runs on both.

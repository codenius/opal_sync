# Course node processing

This note describes the node discovery and processing behavior implemented in
`src/opal_sync/scraper/course_tree.py`, `src/opal_sync/scraper/folder.py`, and
`src/opal_sync/scraper/file_links.py`.

## Node discovery

The scraper first looks for OPAL's embedded jsTree `initial_data` and uses its
icon classes to decide which nodes to record:

| jsTree icon class | Stored node type | Processed |
| --- | --- | --- |
| `icon-st` | `structure` | Yes |
| `icon-bc` | `folder` | Yes |
| `icon-ta` | `task` | Yes |
| Any other icon | — | No |

Children of every tree item are still traversed, including children of items
that are not recorded. The recorded structure, folder, or task node's URL is
then fetched by the sync engine. A top-level structure node titled
“Unterlagen” is recorded, but its title is omitted from the local relative
path.

If the jsTree data is missing, invalid, or produces no recorded nodes, the
scraper falls back to links whose URL contains `/CourseNode/{id}`. This
fallback records every unique matching link; it does not filter by icon. It
infers a type from the link and icon CSS classes (`folder`, `singlepage`,
`dialog`, or `task`) and defaults to `folder` when no known marker is present.

## Fetching content

The sync engine processes every discovered node; it does not apply another
node-type allowlist. It opens the node URL, extracts files from its page, and
walks nested folders found in folder listings. A node that has no extractable
files simply contributes no files.

Task nodes receive additional handling for returned files. The returnbox uses
`javascript:;` links wired to Wicket GET actions. The scraper pairs those links
with their Wicket actions, uses the displayed filename to identify the file,
and creates a stable per-file sync key. The downloader handles Wicket request
methods and Fibercode URLs generically: it sends page-bound GET actions before
any preliminary request and follows a Fibercode URL returned in either an XML
redirect or Wicket's AJAX evaluate script.

Structure nodes can also show recent-file shortcuts after their
overview AJAX request loads. These links use the same `javascript:;` and Wicket
GET pattern, but they point to files already present in their actual course
folders. They are not treated as new files: the folder fetcher does not trigger
the overview's timer AJAX request, the ordinary anchor pass skips
`javascript:` URLs, and the special file-link extraction is enabled only for
task nodes. The generic Wicket downloader only runs for file requests the
scraper has extracted.

File extension include and exclude settings are applied during file syncing;
they do not determine which course nodes are discovered.
